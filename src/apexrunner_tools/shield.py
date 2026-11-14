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

The score is a heuristic over live market structure (liquidations, crowding,
funding, slippage). It is not a prediction, a guarantee or investment advice.
A trade that passes is not "safe".
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from .client import ApexClient, ApexResult
from .errors import ApexError, TradeBlocked

SIGNAL = "check_trade_safety"
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

    @property
    def checked(self) -> bool:
        return self.error is None

    @property
    def message(self) -> str:
        trade = f"{self.side} {self.coin} ${self.size_usd:,.0f}" if _is_num(self.size_usd) else (
            f"{self.side} {self.coin} {self.size_usd}")
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
    """

    def __init__(
        self,
        client: ApexClient,
        *,
        block_below: int = DANGER_BELOW,
        fail_open: bool = False,
        on_decision: Callable[[ShieldDecision], None] | None = None,
    ):
        if isinstance(block_below, bool) or not isinstance(block_below, int) or not 1 <= block_below <= 100:
            raise ValueError("block_below must be an int from 1 to 100")
        self.client = client
        self.block_below = block_below
        self.fail_open = fail_open
        self.on_decision = on_decision

    # ── checks ────────────────────────────────────────────────────────────────

    def check(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        """Score a proposed trade. Never raises for API problems; see ``guard``."""
        try:
            result = self.client.call(SIGNAL, coin=coin, side=side, size_usd=size_usd)
        except (ApexError, ValidationError) as exc:
            return self._emit(self._failed(coin, side, size_usd, exc))
        return self._emit(self._judge(coin, side, size_usd, result))

    async def acheck(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        try:
            result = await self.client.acall(SIGNAL, coin=coin, side=side, size_usd=size_usd)
        except (ApexError, ValidationError) as exc:
            return self._emit(self._failed(coin, side, size_usd, exc))
        return self._emit(self._judge(coin, side, size_usd, result))

    def guard(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        """``check``, then raise TradeBlocked unless allowed."""
        decision = self.check(coin, side, size_usd)
        if not decision.allowed:
            raise TradeBlocked(decision)
        return decision

    async def aguard(self, coin: Any, side: Any, size_usd: Any) -> ShieldDecision:
        decision = await self.acheck(coin, side, size_usd)
        if not decision.allowed:
            raise TradeBlocked(decision)
        return decision

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

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                await self.aguard(*trade_of(args, kwargs))
                return await func(*args, **kwargs)
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.guard(*trade_of(args, kwargs))
            return func(*args, **kwargs)
        return wrapper

    # ── internals ─────────────────────────────────────────────────────────────

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


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # rejects NaN


def apex_shield(client: ApexClient | None = None, *, params: Mapping[str, str] | None = None,
                extract: Callable[..., tuple[Any, Any, Any]] | None = None, block_below: int = DANGER_BELOW,
                fail_open: bool = False, on_decision: Callable[[ShieldDecision], None] | None = None,
                **client_kwargs: Any):
    """One-line decorator: ``@apex_shield(private_key=...)`` or ``@apex_shield(client)``.

    Creates its own RiskShield (and ApexClient, if not given). To share one wallet,
    budget and cache across several functions, create one RiskShield and use
    ``@shield.protect`` instead.
    """
    if client is None:
        client = ApexClient(**client_kwargs)
    elif client_kwargs:
        raise ValueError("Pass either client or client arguments, not both.")
    shield = RiskShield(client, block_below=block_below, fail_open=fail_open, on_decision=on_decision)
    return shield.protect(params=params, extract=extract)


__all__ = ["RiskShield", "ShieldDecision", "TradeBlocked", "apex_shield"]
