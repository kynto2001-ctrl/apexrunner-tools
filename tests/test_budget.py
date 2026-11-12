"""Spend budget: enforced at signing time, per signature, thread-safe, conservative."""

import threading

import pytest

from apexrunner_tools import ApexUnavailable, BudgetExceeded, PaymentRejected
from apexrunner_tools.payment import SpendBudget, build_sync_client, call_ledger
from tests.conftest import b64json

SLIP = dict(coin="ETH", side="sell")


def test_budget_stops_signing_when_spent(mock_apex, make_client):
    client = make_client(cache=False, budget_usdc=0.014)  # exactly 2 calls at $0.007
    client.call("slippage_forecast", size_usd=1000, **SLIP)
    client.call("slippage_forecast", size_usd=2000, **SLIP)
    with pytest.raises(BudgetExceeded) as exc:
        client.call("slippage_forecast", size_usd=3000, **SLIP)
    assert "Nothing was signed" in str(exc.value)
    assert len(mock_apex.signed) == 2
    assert client.budget.spent_usdc == pytest.approx(0.014)
    assert client.budget.remaining_usdc == pytest.approx(0)


async def test_budget_applies_to_async_calls(mock_apex, make_client):
    client = make_client(cache=False, budget_usdc=0.007)
    await client.acall("slippage_forecast", size_usd=1000, **SLIP)
    with pytest.raises(BudgetExceeded):
        await client.acall("slippage_forecast", size_usd=2000, **SLIP)
    assert len(mock_apex.signed) == 1


async def test_sync_and_async_share_one_budget(mock_apex, make_client):
    client = make_client(cache=False, budget_usdc=0.007)
    client.call("slippage_forecast", size_usd=1000, **SLIP)
    with pytest.raises(BudgetExceeded):
        await client.acall("slippage_forecast", size_usd=2000, **SLIP)
    assert len(mock_apex.signed) == 1


def test_zero_budget_signs_nothing(mock_apex, make_client):
    client = make_client(budget_usdc=0)
    with pytest.raises(BudgetExceeded):
        client.call("risk_assessment_bundle")
    assert mock_apex.signed == []


def test_default_budget_is_one_dollar(make_client):
    assert make_client().budget.limit_usdc == pytest.approx(1.00)


def test_concurrent_calls_never_overspend(mock_apex, make_client):
    client = make_client(cache=False, budget_usdc=0.021)  # room for exactly 3
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            client.call("slippage_forecast", size_usd=1000 + i, **SLIP)
            res = "ok"
        except BudgetExceeded:
            res = "budget"
        with lock:
            outcomes.append(res)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 3
    assert outcomes.count("budget") == 9
    assert len(mock_apex.signed) == 3
    assert client.budget.spent_usdc == pytest.approx(0.021)


def test_signed_but_failed_call_still_counts(mock_apex, make_client):
    """Conservative: once signed, it is spent, even if the server then errors."""
    mock_apex.paid_response = lambda p, q: (503, {"error": "inputs unavailable", "charged": False}, {})
    client = make_client(cache=False)
    with pytest.raises(ApexUnavailable) as exc:
        client.call("slippage_forecast", size_usd=1000, **SLIP)
    assert exc.value.status_code == 503
    assert len(mock_apex.signed) == 1
    assert client.budget.spent_usdc == pytest.approx(0.007)


def test_rejected_payment_reports_reason_and_counts(mock_apex, make_client):
    def reject(p, q):
        ch = dict(mock_apex.challenge, error="insufficient_funds")
        return 402, {"error": "insufficient_funds"}, {"PAYMENT-REQUIRED": b64json(ch)}

    mock_apex.paid_response = reject
    client = make_client(cache=False)
    with pytest.raises(PaymentRejected) as exc:
        client.call("slippage_forecast", size_usd=1000, **SLIP)
    assert "insufficient_funds" in str(exc.value)
    assert client.address in str(exc.value)
    assert client.budget.spent_usdc == pytest.approx(0.007)


def test_every_signature_is_charged_not_every_call(account):
    """The SDK can re-sign within one HTTP call (recovery). Each signature must hit the budget."""
    from eth_account import Account  # noqa: F401
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentAbortedError, PaymentRequired

    from tests.conftest import real_challenge

    budget = SpendBudget(0.014)
    x402 = build_sync_client(EthAccountSigner(account), budget, max_atomic=20_000)
    challenge = PaymentRequired.model_validate(real_challenge())
    ledger: list[int] = []
    token = call_ledger.set(ledger)
    try:
        x402.create_payment_payload(challenge)
        x402.create_payment_payload(challenge)  # the "recovery" re-sign
        with pytest.raises(PaymentAbortedError):
            x402.create_payment_payload(challenge)
    finally:
        call_ledger.reset(token)
    assert ledger == [7000, 7000]
    assert budget.spent_usdc == pytest.approx(0.014)


def test_spend_budget_rejects_negative():
    with pytest.raises(ValueError):
        SpendBudget(-0.01)


def test_budget_is_atomic_under_heavy_contention():
    """Many threads racing on try_spend must never overshoot the limit.

    Honest limit of this test: on GIL builds of CPython (3.11/3.12) the
    unlocked read-modify-write cannot actually interleave, so removing the
    lock does NOT make this fail there (mutation-tested 2026-11-12). The lock
    exists for free-threaded builds (3.13t+), where this test is meaningful.
    """
    import sys

    budget = SpendBudget(0.05)  # 50_000 atomic units
    granted = [0] * 32
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def worker(i):
            for _ in range(5_000):
                if budget.try_spend(1):
                    granted[i] += 1

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert sum(granted) == 50_000
    assert budget.spent_usdc == pytest.approx(0.05)
