"""ApexClient: one paid call per signal, with caching, budget and error mapping."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import requests
from x402.http import decode_payment_required_header, decode_payment_response_header
from x402.http.clients import wrapRequestsWithPayment, x402HttpxClient

from .constants import (
    APEX_BASE_URL,
    DEFAULT_BUDGET_USDC,
    DEFAULT_MAX_PAYMENT_USDC,
    MAX_ALLOWED_PAYMENT_USDC,
    MAX_CACHE_SECONDS,
    MAX_CONTENT_CHARS,
    REQUEST_TIMEOUT_S,
    USER_AGENT,
)
from .errors import ApexResponseError, ApexUnavailable, PaymentRejected
from .payment import (
    SpendBudget,
    build_async_client,
    build_sync_client,
    call_ledger,
    from_atomic,
    map_payment_exception,
    resolve_signer,
    to_atomic,
)
from .signals import SignalSpec, get_spec


@dataclass(frozen=True)
class ApexResult:
    signal: str
    data: dict[str, Any]
    cached: bool = False
    #: USDC signed for this call (0.0 when served from cache).
    amount_usdc: float = 0.0
    transaction: str | None = None
    payer: str | None = None
    network: str | None = None
    query: dict[str, str] = field(default_factory=dict)

    def payment(self) -> dict[str, Any]:
        return {
            "amount_usdc": self.amount_usdc,
            "transaction": self.transaction,
            "network": self.network,
            "cached": self.cached,
        }

    def to_content(self, max_chars: int = MAX_CONTENT_CHARS) -> str:
        """Compact JSON for the model."""
        out = json.dumps(
            {"signal": self.signal, "data": self.data, "payment": self.payment()},
            separators=(",", ":"),
            default=str,
        )
        return out if len(out) <= max_chars else out[:max_chars] + "...[truncated]"

    def to_artifact(self) -> dict[str, Any]:
        """Full result for the application (not sent to the model)."""
        return {
            "signal": self.signal,
            "query": self.query,
            "data": self.data,
            "payment": {**self.payment(), "payer": self.payer},
        }


class _TTLCache:
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._items: dict[tuple, tuple[float, ApexResult]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple) -> ApexResult | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            if hit[0] <= self._clock():
                del self._items[key]
                return None
            return hit[1]

    def put(self, key: tuple, result: ApexResult, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._items[key] = (self._clock() + ttl, result)


def _ttl_for(data: dict[str, Any]) -> float:
    """Cache for as long as the response says it is valid, minus its age, capped."""
    valid = data.get("valid_for_seconds")
    if not isinstance(valid, (int, float)) or isinstance(valid, bool) or valid <= 0:
        return 0.0
    age = data.get("data_age_seconds", 0)
    if not isinstance(age, (int, float)) or isinstance(age, bool) or age < 0:
        age = 0
    if data.get("stale") is True:
        return 0.0
    return float(min(valid - age, MAX_CACHE_SECONDS))


class ApexClient:
    """Pays for and fetches APEX Runner signals.

    Supply the wallet as exactly one of ``private_key``, ``account`` (an
    eth_account LocalAccount) or ``signer`` (any object with ``address`` and
    ``sign_typed_data``). The wallet needs USDC on Base mainnet; no ETH is
    needed, because payments are signed authorizations settled by the facilitator.
    """

    def __init__(
        self,
        *,
        private_key: str | None = None,
        account: Any = None,
        signer: Any = None,
        max_payment_usdc: float = DEFAULT_MAX_PAYMENT_USDC,
        budget_usdc: float = DEFAULT_BUDGET_USDC,
        cache: bool = True,
        timeout: float = REQUEST_TIMEOUT_S,
        base_url: str = APEX_BASE_URL,
        _clock: Callable[[], float] = time.monotonic,
    ):
        if not (0 < max_payment_usdc <= MAX_ALLOWED_PAYMENT_USDC):
            raise ValueError(f"max_payment_usdc must be in (0, {MAX_ALLOWED_PAYMENT_USDC}]")
        self._signer = resolve_signer(private_key, account, signer)
        self.address: str = self._signer.address
        self.budget = SpendBudget(budget_usdc)
        self._max_atomic = to_atomic(max_payment_usdc)
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._cache = _TTLCache(_clock) if cache else None
        self._session = wrapRequestsWithPayment(
            requests.Session(), build_sync_client(self._signer, self.budget, self._max_atomic)
        )
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.headers["Accept"] = "application/json"
        self._async_x402 = build_async_client(self._signer, self.budget, self._max_atomic)

    # ── public API ─────────────────────────────────────────────────────────────

    def call(self, signal: str, **params: Any) -> ApexResult:
        spec, query, key = self._prepare(signal, params)
        hit = self._cached(key)
        if hit is not None:
            return hit
        ledger: list[int] = []
        token = call_ledger.set(ledger)
        try:
            resp = self._session.get(self._url(spec), params=query, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - mapped to ApexError below
            raise map_payment_exception(exc, self.budget) from exc
        finally:
            call_ledger.reset(token)
        return self._finish(spec, query, key, resp.status_code, resp.headers, resp.content, ledger)

    async def acall(self, signal: str, **params: Any) -> ApexResult:
        spec, query, key = self._prepare(signal, params)
        hit = self._cached(key)
        if hit is not None:
            return hit
        ledger: list[int] = []
        token = call_ledger.set(ledger)
        try:
            # A fresh AsyncClient per call: httpx clients are bound to an event loop,
            # and agent frameworks do not guarantee the same loop between calls.
            async with x402HttpxClient(
                self._async_x402,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as http:
                resp = await http.get(self._url(spec), params=query)
                content = resp.content
        except Exception as exc:  # noqa: BLE001
            raise map_payment_exception(exc, self.budget) from exc
        finally:
            call_ledger.reset(token)
        return self._finish(spec, query, key, resp.status_code, resp.headers, content, ledger)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "ApexClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── internals ──────────────────────────────────────────────────────────────

    def _url(self, spec: SignalSpec) -> str:
        return f"{self._base_url}/{spec.slug}"

    def _prepare(self, signal: str, params: dict[str, Any]):
        spec = get_spec(signal)
        args = spec.input_model(**params)
        query = spec.to_query(args)
        return spec, query, (spec.slug, tuple(sorted(query.items())))

    def _cached(self, key: tuple) -> ApexResult | None:
        if self._cache is None:
            return None
        hit = self._cache.get(key)
        return None if hit is None else replace(hit, cached=True, amount_usdc=0.0)

    def _finish(
        self,
        spec: SignalSpec,
        query: dict[str, str],
        key: tuple,
        status: int,
        headers: Any,
        content: bytes,
        ledger: list[int],
    ) -> ApexResult:
        signed = from_atomic(sum(ledger))
        if status == 402:
            reason = _payment_error_reason(headers)
            raise PaymentRejected(
                f"Payment was rejected{f' ({reason})' if reason else ''}. Check that wallet "
                f"{self.address} holds USDC on Base mainnet (about $0.01 per call)."
            )
        if not 200 <= status < 300:
            body = content[:300].decode("utf-8", "replace")
            raise ApexUnavailable(
                f"APEX Runner returned HTTP {status} for {spec.name}; x402 settles only on "
                f"success, so this call should not be charged. Body: {body}",
                status_code=status,
            )
        try:
            data = json.loads(content)
        except ValueError:
            raise ApexResponseError(f"APEX Runner returned a non-JSON body for {spec.name}.") from None
        if not isinstance(data, dict):
            raise ApexResponseError(f"APEX Runner returned a non-object JSON body for {spec.name}.")

        receipt = _settle_receipt(headers)
        result = ApexResult(
            signal=spec.name,
            data=data,
            amount_usdc=signed,
            transaction=receipt.get("transaction"),
            payer=receipt.get("payer"),
            network=receipt.get("network"),
            query=query,
        )
        if self._cache is not None:
            self._cache.put(key, result, _ttl_for(data))
        return result


def _header(headers: Any, *names: str) -> str | None:
    for name in names:
        value = headers.get(name)
        if value:
            return value
    return None


def _settle_receipt(headers: Any) -> dict[str, Any]:
    raw = _header(headers, "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE")
    if not raw:
        return {}
    try:
        settle = decode_payment_response_header(raw)
        return {
            "transaction": getattr(settle, "transaction", None),
            "payer": getattr(settle, "payer", None),
            "network": getattr(settle, "network", None),
        }
    except Exception:  # noqa: BLE001 - a receipt we cannot read is not a failed call
        return {}


def _payment_error_reason(headers: Any) -> str | None:
    raw = _header(headers, "PAYMENT-REQUIRED")
    if not raw:
        return None
    try:
        return getattr(decode_payment_required_header(raw), "error", None)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["ApexClient", "ApexResult"]
