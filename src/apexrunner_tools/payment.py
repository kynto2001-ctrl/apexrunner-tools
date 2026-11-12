"""Wallet setup, the APEX-only payment policy, and the spend budget.

Two independent gates run before anything is signed:

1. A payment *policy* removes every payment option that is not exact-scheme
   USDC on Base, to APEX's pinned address, at or under the per-call cap. If no
   option is left, x402 raises and nothing is signed.
2. A *before-payment hook* re-checks the selected option and charges the spend
   budget. If the budget would be exceeded it aborts, and nothing is signed.

The hook runs once per signature, not once per call, so the SDK's "recovery"
path (which can sign a second payment within one HTTP call) is budgeted too.
A signed authorization counts as spent the moment it is signed: the budget
never assumes a payment was *not* settled.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from x402 import x402Client, x402ClientSync
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import AbortResult, NoMatchingRequirementsError, PaymentAbortedError

from .constants import APEX_PAY_TO, BASE_MAINNET, BASE_USDC, USDC_DECIMALS
from .errors import ApexError, BudgetExceeded, PaymentRefused

ABORT_BUDGET = "apexrunner_budget_exceeded"
ABORT_NOT_APEX = "apexrunner_not_apex_payment"

_UNIT = Decimal(10) ** USDC_DECIMALS


def to_atomic(usdc: float | int | str | Decimal) -> int:
    return int((Decimal(str(usdc)) * _UNIT).to_integral_value())


def from_atomic(atomic: int) -> float:
    return float(Decimal(atomic) / _UNIT)


# Amounts signed during the current call. Set by ApexClient around each request.
# Works for threads (sync) and asyncio tasks, including hooks the async client
# runs through asyncio.to_thread (which copies the context).
call_ledger: ContextVar[list[int] | None] = ContextVar("apexrunner_call_ledger", default=None)


def resolve_signer(private_key: str | None = None, account: Any = None, signer: Any = None) -> Any:
    """Return an x402 EVM signer from exactly one of the three ways to supply a wallet.

    A private key becomes an eth_account LocalAccount, which holds it only to sign;
    the key string itself is not stored anywhere else.
    """
    supplied = sum(x is not None for x in (private_key, account, signer))
    if supplied != 1:
        raise ValueError("Provide exactly one of private_key, account or signer.")
    if private_key is not None:
        from eth_account import Account

        account = Account.from_key(private_key)
    if account is not None:
        return EthAccountSigner(account)
    if not isinstance(getattr(signer, "address", None), str) or not callable(
        getattr(signer, "sign_typed_data", None)
    ):
        raise TypeError("signer must have an 'address' string and a 'sign_typed_data' method.")
    return signer


def is_apex_requirement(req: Any, max_atomic: int) -> bool:
    amount = str(getattr(req, "amount", "") or "")
    if not amount.isdigit():
        return False
    return (
        getattr(req, "scheme", None) == "exact"
        and getattr(req, "network", None) == BASE_MAINNET
        and str(getattr(req, "asset", "")).lower() == BASE_USDC.lower()
        and str(getattr(req, "pay_to", "")).lower() == APEX_PAY_TO.lower()
        and 0 < int(amount) <= max_atomic
    )


def apex_only_policy(max_atomic: int):
    def policy(version: int, reqs: list) -> list:
        if version != 2:  # APEX speaks x402 v2; never guess at v1 fields
            return []
        return [r for r in reqs if is_apex_requirement(r, max_atomic)]

    return policy


class SpendBudget:
    """Thread-safe running total of USDC signed by one client."""

    def __init__(self, limit_usdc: float):
        if limit_usdc < 0:
            raise ValueError("budget_usdc must be >= 0")
        self._limit = to_atomic(limit_usdc)
        self._spent = 0
        self._lock = threading.Lock()

    def try_spend(self, atomic: int) -> bool:
        with self._lock:
            if self._spent + atomic > self._limit:
                return False
            self._spent += atomic
            return True

    @property
    def limit_usdc(self) -> float:
        return from_atomic(self._limit)

    @property
    def spent_usdc(self) -> float:
        with self._lock:
            return from_atomic(self._spent)

    @property
    def remaining_usdc(self) -> float:
        with self._lock:
            return from_atomic(self._limit - self._spent)


def _before_payment_hook(budget: SpendBudget, max_atomic: int):
    def hook(ctx: Any) -> AbortResult | None:
        req = ctx.selected_requirements
        if ctx.payment_required.x402_version != 2 or not is_apex_requirement(req, max_atomic):
            return AbortResult(reason=ABORT_NOT_APEX)
        amount = int(req.amount)
        if not budget.try_spend(amount):
            return AbortResult(reason=ABORT_BUDGET)
        ledger = call_ledger.get()
        if ledger is not None:
            ledger.append(amount)
        return None

    return hook


def build_sync_client(signer: Any, budget: SpendBudget, max_atomic: int) -> x402ClientSync:
    client = x402ClientSync()
    register_exact_evm_client(client, signer, networks=BASE_MAINNET)
    client.register_policy(apex_only_policy(max_atomic))
    client.on_before_payment_creation(_before_payment_hook(budget, max_atomic))
    return client


def build_async_client(signer: Any, budget: SpendBudget, max_atomic: int) -> x402Client:
    client = x402Client()
    register_exact_evm_client(client, signer, networks=BASE_MAINNET)
    client.register_policy(apex_only_policy(max_atomic))
    client.on_before_payment_creation(_before_payment_hook(budget, max_atomic))
    return client


def map_payment_exception(exc: BaseException, budget: SpendBudget) -> ApexError:
    """Translate x402's exception (often wrapped by the HTTP adapter) into ours."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, PaymentAbortedError):
            if cur.reason == ABORT_BUDGET:
                return BudgetExceeded(
                    f"Spend budget reached (${budget.spent_usdc:.4f} of ${budget.limit_usdc:.2f} "
                    "signed). Nothing was signed for this call."
                )
            return PaymentRefused(
                "The payment request did not match APEX Runner's pinned terms. Nothing was signed."
            )
        if isinstance(cur, NoMatchingRequirementsError):
            return PaymentRefused(
                "No acceptable payment option: APEX payments must be exact USDC on Base to the "
                "pinned address at or under the per-call cap. Nothing was signed."
            )
        cur = cur.__cause__ or cur.__context__
    return ApexError(f"Request to APEX Runner failed: {type(exc).__name__}: {exc}")
