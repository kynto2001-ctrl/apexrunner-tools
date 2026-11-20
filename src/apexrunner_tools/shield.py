"""RiskShield: a pre-trade gate in code, not in the prompt.

    from apexrunner_tools import ApexClient
    from apexrunner_tools.shield import RiskShield

    shield = RiskShield(ApexClient(private_key=os.environ["AGENT_WALLET_KEY"]))

    @shield.protect
    def place_order(coin: str, side: str, size_usd: float) -> dict:
        return exchange.market_order(coin, side, size_usd)

    place_order("ETH", "long", 5000)   # checks first; raises TradeBlocked on DANGER

Before the wrapped function runs, the shield pays for one ``check_trade_safety``
call (about $0.014) and blocks the trade if the safety score is under
``block_below`` (default 40, the DANGER band) or the verdict is DANGER. A
blocked trade never reaches the wrapped function.

Fails CLOSED by default: if the check cannot be completed (APEX unavailable,
budget spent, payment refused, unsupported coin, unreadable response), the
trade is blocked. Pass ``fail_open=True`` to let trades through when the check
is unavailable; the decision still records why.

Local risk limits (0.3.0): pass ``limits=`` (a path to apex_risk.toml, or a
``RiskLimits``) and the shield first enforces the developer's own caps: max size
per trade, max USD traded per rolling 24 h, and a portfolio auto-stop. They run
before the paid check, cost nothing, and a trade that breaks one raises
``LocalLimitBlocked`` ("Blocked by local risk limits: ...") without paying.
They always fail closed, whatever ``fail_open`` says: an unreadable state file
or a failing ``equity_fn`` blocks the trade. They are enforced in this process
and are not tamper-proof; see the README.

The score is a heuristic over live market structure (liquidations, crowding,
funding, slippage). It is not a prediction, a guarantee or investment advice.
A trade that passes is not "safe".
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import math
import os
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from .client import ApexClient, ApexResult
from .errors import ApexError, LocalLimitBlocked, TradeBlocked
from .ledger import LedgerError, Reservation, StopLatch, StopState, VolumeExceeded, VolumeLedger
from .limits import RiskLimits, load_limits

SIGNAL = "check_trade_safety"
LOCAL_BLOCK_PREFIX = "Blocked by local risk limits"
STAGE_LOCAL = "local_limits"
STAGE_SAFETY = "safety_check"
DANGER_BELOW = 40
_TRADE_FIELDS = ("coin", "side", "size_usd")


@dataclass(frozen=True)
class ShieldDecision:
    allowed: bool
    coin: Any
    side: Any
    size_usd: Any
    #: 0-100, higher = lower risk. None when the check could not be completed.
    score: int | None = None
    #: LOW_RISK, CAUTION or DANGER. None when the check could not be completed.
    verdict: str | None = None
    summary: str | None = None
    #: One line per risk factor, highest risk first.
    reasons: tuple[str, ...] = ()
    #: Why the check could not be completed, if it could not.
    error: str | None = None
    threshold: int = DANGER_BELOW
    result: ApexResult | None = field(default=None, repr=False)
    #: "local_limits" (blocked by apex_risk.toml limits; no paid check ran) or "safety_check".
    stage: str = STAGE_SAFETY
    #: For local blocks: max_trade_usd, max_24h_volume_usd, portfolio_stop_pct,
    #: invalid_trade, or risk_state (state file unreadable).
    limit: str | None = None

    @property
    def checked(self) -> bool:
        """True when check_trade_safety ran and returned a usable score."""
        return self.stage == STAGE_SAFETY and self.error is None

    @property
    def message(self) -> str:
        trade = f"{self.side} {self.coin} ${self.size_usd:,.2f}".removesuffix(".00") if _is_num(self.size_usd) else (
            f"{self.side} {self.coin} {self.size_usd}")
        if self.stage == STAGE_LOCAL:
            return f"{LOCAL_BLOCK_PREFIX}: {trade}: {self.error or self.summary}"
        if self.error is not None:
            state = "ALLOWED (fail_open)" if self.allowed else "BLOCKED"
            return f"{state}: {trade} could not be risk-checked: {self.error}"
        state = "ALLOWED" if self.allowed else "BLOCKED"
        head = f"{state}: {trade} scored {self.score}/100 ({self.verdict}; blocks under {self.threshold})."
        return f"{head} {self.summary}" if self.summary else head


class RiskShield:
    """Gate trades on APEX Runner's check_trade_safety score.

    Args:
        client: the ApexClient that pays for checks (its wallet, budget and cache).
        block_below: block when safety_score < this. Default 40 = block DANGER only.
            65 also blocks CAUTION. Must be 1-100.
        fail_open: if True, allow the trade when the check cannot be completed.
            Default False (block).
        on_decision: optional callback, called with every ShieldDecision (log it,
            alert on it). Exceptions it raises propagate.
        limits: local risk limits, checked before the paid call. A path to an
            apex_risk.toml, or a RiskLimits (e.g. ``load_limits()``). Default: none.
        equity_fn: required when ``portfolio_stop_pct`` is set, refused otherwise.
            A plain (non-async) function returning current portfolio equity in USD
            across every venue the stop should cover. Called once per trade.
    """

    def __init__(
        self,
        client: ApexClient,
        *,
        block_below: int = DANGER_BELOW,
        fail_open: bool = False,
        on_decision: Callable[[ShieldDecision], None] | None = None,
        limits: RiskLimits | str | os.PathLike[str] | None = None,
        equity_fn: Callable[[], float] | None = None,
        _clock: Callable[[], float] = time.time,
    ):
        if isinstance(block_below, bool) or not isinstance(block_below, int) or not 1 <= block_below <= 100:
            raise ValueError("block_below must be an int from 1 to 100")
        if isinstance(limits, (str, os.PathLike)):
            limits = load_limits(limits)
        if limits is not None and not isinstance(limits, RiskLimits):
            raise TypeError("limits must be a RiskLimits or a path to apex_risk.toml")
        if limits is not None and not limits.active:
            raise ValueError("limits sets no limit; pass limits=None to run without local limits")
        stop = limits is not None and limits.portfolio_stop_pct is not None
        if stop and equity_fn is None:
            raise ValueError("portfolio_stop_pct is set but equity_fn is missing: the stop cannot be "
                             "evaluated without current equity, so every trade would be blocked")
        if equity_fn is not None and not stop:
            raise ValueError("equity_fn was given but no portfolio_stop_pct is configured; "
                             "set the stop or drop equity_fn")
        if equity_fn is not None and (not callable(equity_fn) or inspect.iscoroutinefunction(equity_fn)):
            raise TypeError("equity_fn must be a plain (non-async) callable returning USD equity")
        self.client = client
        self.block_below = block_below
        self.fail_open = fail_open
        self.on_decision = on_decision
        self.limits = limits
        self.equity_fn = equity_fn
        self._ledger = (VolumeLedger(limits.state_path, clock=_clock)
                        if limits is not None and limits.max_24h_volume_usd is not None else None)
        self._latch = StopLatch(limits.state_path, clock=_clock) if stop else None

    # ── checks ────────────────────────────────────────────────────────────────

    def check(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        """Local limits (read-only), then the paid score. Never raises for API problems; see ``guard``.

        ``check`` and ``guard`` do not add to the 24 h volume; only ``protect`` does.
        If you place orders yourself after ``guard``, count them with ``record_volume``.
        """
        blocked, _ = self._local(coin, side, size_usd, reserve=False)
        return blocked or self._paid(coin, side, size_usd)

    async def acheck(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        blocked, _ = await asyncio.to_thread(self._local, coin, side, size_usd, False)
        return blocked or await self._apaid(coin, side, size_usd)

    def guard(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        """``check``, then raise TradeBlocked (LocalLimitBlocked for a local limit) unless allowed."""
        return _raise_unless_allowed(self.check(coin, side, size_usd))

    async def aguard(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        return _raise_unless_allowed(await self.acheck(coin, side, size_usd))

    # ── local limit state ─────────────────────────────────────────────────────

    def reset_stop(self) -> StopState:
        """Re-enable trading after the portfolio stop tripped, with current equity
        as the new baseline. Also call it after deposits or withdrawals, which
        otherwise read as gains or losses against the old baseline."""
        if self._latch is None or self.equity_fn is None:
            raise ValueError("no portfolio_stop_pct configured")
        return self._latch.reset(self.equity_fn())

    def record_volume(self, size_usd: float, *, coin: Any = None, side: Any = None) -> None:
        """Count a trade placed outside ``protect`` (e.g. after a manual ``guard``) toward the 24 h volume."""
        if self._ledger is not None:
            self._ledger.record(size_usd, coin=coin, side=side)

    def local_status(self) -> dict[str, Any]:
        """Current limits and state: 24 h volume used/remaining, stop baseline and trip. Raises LedgerError."""
        if self.limits is None:
            return {"limits": None}
        out: dict[str, Any] = {"limits": self.limits.describe(), "state_path": str(self.limits.state_path)}
        if self._ledger is not None:
            used = self._ledger.used_usd()
            out["volume_24h_used_usd"] = used
            out["volume_24h_remaining_usd"] = max(0.0, self.limits.max_24h_volume_usd - used)  # type: ignore[operator]
        if self._latch is not None:
            out["stop"] = self._latch.status()
        return out

    # ── decorator ─────────────────────────────────────────────────────────────

    def protect(
        self,
        func: Callable | None = None,
        *,
        params: Mapping[str, str] | None = None,
        extract: Callable[..., tuple[Any, Any, Any]] | None = None,
    ):
        """Decorate a trade-placing function (sync or async) so it is checked first.

        By default the function's own ``coin``, ``side`` and ``size_usd`` arguments
        are used. Rename them with ``params={"coin": "symbol", "side": "direction",
        "size_usd": "notional"}``, or pass ``extract=lambda *a, **kw: (coin, side,
        size_usd)`` for anything else (e.g. an order object).
        """
        if func is None:
            return functools.partial(self.protect, params=params, extract=extract)
        if params is not None and extract is not None:
            raise ValueError("Pass params or extract, not both.")
        names = {**{k: k for k in _TRADE_FIELDS}, **(params or {})}
        unknown = set(names) - set(_TRADE_FIELDS)
        if unknown:
            raise ValueError(f"params keys must be among {_TRADE_FIELDS}, got {sorted(unknown)}")
        sig = inspect.signature(func)
        if extract is None:
            missing = [n for n in names.values() if n not in sig.parameters]
            if missing:
                raise TypeError(f"{func.__qualname__} has no parameter(s) {missing}; pass params= or extract=")

        def trade_of(args: tuple, kwargs: dict) -> tuple[Any, Any, Any]:
            if extract is not None:
                return extract(*args, **kwargs)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return tuple(bound.arguments[names[k]] for k in _TRADE_FIELDS)  # type: ignore[return-value]

        # Order of operations, per call:
        #   1. local limits (free). A pass reserves the size in the 24 h ledger.
        #   2. check_trade_safety (paid). A block releases the reservation.
        #   3. the wrapped function. Success commits the reservation; an exception
        #      keeps it counted ("unknown"): the order may have reached the venue.
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                trade = trade_of(args, kwargs)
                blocked, res = await asyncio.to_thread(self._local, *trade, True)
                if blocked:
                    raise LocalLimitBlocked(blocked)
                try:
                    _raise_unless_allowed(await self._apaid(*trade))
                except BaseException:
                    await asyncio.to_thread(self._settle, res, "release")
                    raise
                try:
                    out = await func(*args, **kwargs)
                except BaseException:
                    await asyncio.to_thread(self._settle, res, "unknown")
                    raise
                await asyncio.to_thread(self._settle, res, "commit")
                return out
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            trade = trade_of(args, kwargs)
            blocked, res = self._local(*trade, reserve=True)
            if blocked:
                raise LocalLimitBlocked(blocked)
            try:
                _raise_unless_allowed(self._paid(*trade))
            except BaseException:
                self._settle(res, "release")
                raise
            try:
                out = func(*args, **kwargs)
            except BaseException:
                self._settle(res, "unknown")
                raise
            self._settle(res, "commit")
            return out
        return wrapper

    # ── internals ─────────────────────────────────────────────────────────────

    def _paid(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        try:
            result = self.client.call(SIGNAL, coin=coin, side=side, size_usd=size_usd)
        except (ApexError, ValidationError) as exc:
            return self._emit(self._failed(coin, side, size_usd, exc))
        return self._emit(self._judge(coin, side, size_usd, result))

    async def _apaid(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        try:
            result = await self.client.acall(SIGNAL, coin=coin, side=side, size_usd=size_usd)
        except (ApexError, ValidationError) as exc:
            return self._emit(self._failed(coin, side, size_usd, exc))
        return self._emit(self._judge(coin, side, size_usd, result))

    def _local(self, coin: Any, side: Any, size_usd: Any, reserve: bool) -> tuple[ShieldDecision | None, Reservation | None]:
        """Evaluate local limits. Returns (blocking decision, None) or (None, reservation-or-None).

        Cheapest and side-effect-free checks first; the volume reservation, the only
        write, comes last so nothing is reserved for a trade another limit refuses.
        Always fails closed.
        """
        lim = self.limits
        if lim is None:
            return None, None

        def block(limit: str, detail: str, *, failed: bool = False) -> tuple[ShieldDecision, None]:
            return self._emit(ShieldDecision(
                allowed=False, coin=coin, side=side, size_usd=size_usd, stage=STAGE_LOCAL, limit=limit,
                summary=None if failed else detail, error=detail if failed else None,
                threshold=self.block_below)), None

        if not _is_num(size_usd) or not math.isfinite(size_usd) or size_usd <= 0:
            return block("invalid_trade", f"size_usd must be a positive number to check limits, got {size_usd!r}",
                         failed=True)
        if lim.max_trade_usd is not None and size_usd > lim.max_trade_usd:
            return block("max_trade_usd", f"${size_usd:,.2f} exceeds the ${lim.max_trade_usd:,.2f} max trade size")

        if self._latch is not None and self.equity_fn is not None:
            try:
                equity = self.equity_fn()
            except Exception as exc:  # noqa: BLE001 - any failure to read equity blocks
                return block("portfolio_stop_pct", f"equity_fn raised {type(exc).__name__}: {exc}", failed=True)
            try:
                st = self._latch.evaluate(equity, lim.portfolio_stop_pct)  # type: ignore[arg-type]
            except LedgerError as exc:
                return block("risk_state", str(exc), failed=True)
            except ValueError as exc:
                return block("portfolio_stop_pct", f"equity_fn returned an unusable value: {exc}", failed=True)
            if st.tripped:
                return block("portfolio_stop_pct", (
                    f"portfolio auto-stop tripped: equity down {st.loss_pct:.2f}% from the ${st.baseline_usd:,.2f} "
                    f"baseline (stop {lim.portfolio_stop_pct:g}%). Trading stays stopped until shield.reset_stop()."))

        res = None
        if self._ledger is not None:
            limit_usd = lim.max_24h_volume_usd
            try:
                if reserve:
                    res = self._ledger.reserve(size_usd, limit_usd, coin=coin, side=side)  # type: ignore[arg-type]
                else:
                    used = self._ledger.used_usd()
                    if used + size_usd > limit_usd:  # type: ignore[operator]
                        raise VolumeExceeded(used, size_usd, limit_usd)  # type: ignore[arg-type]
            except VolumeExceeded as exc:
                return block("max_24h_volume_usd", str(exc))
            except LedgerError as exc:
                return block("risk_state", str(exc), failed=True)
        return None, res

    def _settle(self, res: Reservation | None, how: str) -> None:
        """Release/commit/mark a reservation. Never raises: the order outcome is already
        decided, and an exception here could make an agent retry a placed order. If
        the write fails, the row stays 'reserved', which still counts (fails closed)."""
        if res is None or self._ledger is None:
            return
        try:
            {"release": self._ledger.release, "commit": self._ledger.commit,
             "unknown": self._ledger.mark_unknown}[how](res)
        except LedgerError as exc:
            warnings.warn(f"RiskShield could not {how} volume reservation {res.id}: {exc}", RuntimeWarning,
                          stacklevel=3)

    def _emit(self, decision: ShieldDecision) -> ShieldDecision:
        if self.on_decision is not None:
            self.on_decision(decision)
        return decision

    def _failed(self, coin: Any, side: Any, size_usd: Any, exc: Exception) -> ShieldDecision:
        if isinstance(exc, ValidationError):
            reason = "invalid or unsupported trade parameters (check_trade_safety covers BTC, ETH, SOL; " \
                     "long/short; size_usd 10-10,000,000): " + "; ".join(
                         f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        else:
            reason = f"{type(exc).__name__}: {exc}"
        return ShieldDecision(allowed=self.fail_open, coin=coin, side=side, size_usd=size_usd,
                              error=reason, threshold=self.block_below)

    def _judge(self, coin: Any, side: Any, size_usd: Any, result: ApexResult) -> ShieldDecision:
        data = result.data
        score, verdict = data.get("safety_score"), data.get("verdict")
        if not _is_num(score) or not 0 <= score <= 100 or not isinstance(verdict, str):
            return self._failed(coin, side, size_usd, ApexError(
                f"unreadable check_trade_safety response (safety_score={score!r}, verdict={verdict!r})"))
        reasons = tuple(
            f"{r.get('factor')}: {r.get('risk')}/100 risk - {r.get('detail')}"
            for r in data.get("reasoning") or () if isinstance(r, dict)
        )
        allowed = score >= self.block_below and verdict != "DANGER"
        return ShieldDecision(
            allowed=allowed, coin=coin, side=side, size_usd=size_usd, score=int(score), verdict=verdict,
            summary=data.get("summary") if isinstance(data.get("summary"), str) else None,
            reasons=reasons, threshold=self.block_below, result=result,
        )


def _raise_unless_allowed(decision: ShieldDecision) -> ShieldDecision:
    if not decision.allowed:
        raise (LocalLimitBlocked if decision.stage == STAGE_LOCAL else TradeBlocked)(decision)
    return decision


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # rejects NaN


def apex_shield(client: ApexClient | None = None, *, params: Mapping[str, str] | None = None,
                extract: Callable[..., tuple[Any, Any, Any]] | None = None, block_below: int = DANGER_BELOW,
                fail_open: bool = False, on_decision: Callable[[ShieldDecision], None] | None = None,
                limits: RiskLimits | str | os.PathLike[str] | None = None,
                equity_fn: Callable[[], float] | None = None, **client_kwargs: Any):
    """One-line decorator: ``@apex_shield(private_key=...)`` or ``@apex_shield(client)``.

    Creates its own RiskShield (and ApexClient, if not given). To share one wallet,
    budget and cache across several functions, create one RiskShield and use
    ``@shield.protect`` instead.
    """
    if client is None:
        client = ApexClient(**client_kwargs)
    elif client_kwargs:
        raise ValueError("Pass either client or client arguments, not both.")
    shield = RiskShield(client, block_below=block_below, fail_open=fail_open, on_decision=on_decision,
                        limits=limits, equity_fn=equity_fn)
    return shield.protect(params=params, extract=extract)


__all__ = ["LocalLimitBlocked", "RiskShield", "ShieldDecision", "TradeBlocked", "apex_shield"]
