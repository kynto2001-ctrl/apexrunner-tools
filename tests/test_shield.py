"""RiskShield: blocks DANGER, fails closed, never lets a blocked trade reach the wrapped function.

Runs against the local mock x402 server (conftest), so every check goes through
the real client: policy, signing, budget and cache.
"""

from __future__ import annotations

import pytest

from apexrunner_tools import ApexError, RiskShield, ShieldDecision, TradeBlocked, apex_shield


def safety(score, verdict, **extra):
    body = {
        "safety_score": score,
        "verdict": verdict,
        "summary": f"{verdict}: test",
        "reasoning": [{"factor": "liquidation", "risk": 100 - score if isinstance(score, int) else 0,
                       "detail": "cascade fuel", "weight": 0.35}],
        "valid_for_seconds": 120,
        "data_age_seconds": 0,
    }
    body.update(extra)
    return lambda path, query: (200, body, {})


@pytest.fixture
def trades():
    return []


@pytest.fixture
def shield(make_client):
    return RiskShield(make_client())


def protected(shield, trades):
    @shield.protect
    def place_order(coin, side, size_usd, note="x"):
        trades.append((coin, side, size_usd, note))
        return "filled"

    return place_order


# ── the core contract ─────────────────────────────────────────────────────────

def test_danger_blocks_and_order_never_runs(mock_apex, shield, trades):
    mock_apex.paid_response = safety(25, "DANGER")
    place = protected(shield, trades)
    with pytest.raises(TradeBlocked) as ei:
        place("ETH", "long", 5000)
    assert trades == []
    d = ei.value.decision
    assert d.allowed is False and d.score == 25 and d.verdict == "DANGER" and d.checked
    assert "BLOCKED" in str(ei.value) and "25/100" in str(ei.value)


def test_low_risk_passes_through_with_original_args(mock_apex, shield, trades):
    mock_apex.paid_response = safety(80, "LOW_RISK")
    place = protected(shield, trades)
    assert place("ETH", "long", 5000, note="keep") == "filled"
    assert trades == [("ETH", "long", 5000, "keep")]
    q = mock_apex.signed[0]
    assert q["path"].endswith("/check-trade-safety")
    assert q["query"] == {"coin": "ETH", "side": "long", "size_usd": "5000"}


def test_caution_passes_by_default_and_blocks_at_65(mock_apex, make_client, trades):
    mock_apex.paid_response = safety(41, "CAUTION")
    assert protected(RiskShield(make_client()), trades)("BTC", "short", 100) == "filled"
    with pytest.raises(TradeBlocked):
        protected(RiskShield(make_client(), block_below=65), trades)("BTC", "short", 100)
    assert len(trades) == 1


def test_boundary_40_is_allowed_39_is_blocked(mock_apex, make_client):
    mock_apex.paid_response = safety(40, "CAUTION")
    assert RiskShield(make_client()).check("SOL", "long", 50).allowed
    mock_apex.paid_response = safety(39, "DANGER")
    assert not RiskShield(make_client()).check("SOL", "long", 50).allowed


def test_danger_verdict_blocks_even_if_score_disagrees(mock_apex, shield):
    mock_apex.paid_response = safety(55, "DANGER")
    assert not shield.check("ETH", "long", 5000).allowed


# ── fail closed ───────────────────────────────────────────────────────────────

def test_unavailable_blocks_by_default(mock_apex, shield, trades):
    mock_apex.paid_response = lambda p, q: (503, {"error": "signal_computation_failed", "charged": False}, {})
    with pytest.raises(TradeBlocked) as ei:
        protected(shield, trades)("ETH", "long", 5000)
    assert trades == [] and not ei.value.decision.checked
    assert "could not be risk-checked" in str(ei.value) and "503" in str(ei.value)


def test_fail_open_allows_but_records_why(mock_apex, make_client, trades):
    mock_apex.paid_response = lambda p, q: (503, {"error": "x"}, {})
    seen = []
    shield = RiskShield(make_client(), fail_open=True, on_decision=seen.append)
    assert protected(shield, trades)("ETH", "long", 5000) == "filled"
    assert seen[0].allowed and seen[0].error and "fail_open" in seen[0].message


def test_budget_exhausted_blocks_without_signing(mock_apex, make_client, trades):
    mock_apex.paid_response = safety(90, "LOW_RISK")
    shield = RiskShield(make_client(budget_usdc=0.001))
    with pytest.raises(TradeBlocked) as ei:
        protected(shield, trades)("ETH", "long", 5000)
    assert trades == [] and mock_apex.signed == []
    assert "BudgetExceeded" in ei.value.decision.error


@pytest.mark.parametrize("body", [
    {"verdict": "LOW_RISK"},                                   # no score
    {"safety_score": True, "verdict": "LOW_RISK"},             # bool is not a score
    {"safety_score": "90", "verdict": "LOW_RISK"},             # string
    {"safety_score": 150, "verdict": "LOW_RISK"},              # out of range
    {"safety_score": float("nan"), "verdict": "LOW_RISK"},     # NaN
    {"safety_score": 90},                                      # no verdict
])
def test_unreadable_response_blocks(mock_apex, shield, body):
    mock_apex.paid_response = lambda p, q: (200, body, {})
    d = shield.check("ETH", "long", 5000)
    assert not d.allowed and "unreadable" in d.error


@pytest.mark.parametrize("coin,side,size", [("DOGE", "long", 5000), ("ETH", "hold", 5000), ("ETH", "long", 5)])
def test_unsupported_trade_blocks_before_any_request(mock_apex, shield, trades, coin, side, size):
    with pytest.raises(TradeBlocked) as ei:
        protected(shield, trades)(coin, side, size)
    assert trades == [] and mock_apex.requests == []
    assert "unsupported" in ei.value.decision.error


# ── inputs and wiring ─────────────────────────────────────────────────────────

def test_buy_sell_and_case_are_normalised(mock_apex, shield):
    mock_apex.paid_response = safety(80, "LOW_RISK")
    shield.check("eth", "BUY", 5000)
    shield.check("eth", "sell", 5000)
    assert [r["query"]["side"] for r in mock_apex.signed] == ["long", "short"]
    assert {r["query"]["coin"] for r in mock_apex.signed} == {"ETH"}


def test_repeat_check_is_cached_and_free(mock_apex, shield):
    mock_apex.paid_response = safety(80, "LOW_RISK")
    a, b = shield.check("ETH", "long", 5000), shield.check("ETH", "long", 5000)
    assert len(mock_apex.signed) == 1 and b.result.cached and b.result.amount_usdc == 0.0


def test_renamed_params(mock_apex, shield, trades):
    mock_apex.paid_response = safety(10, "DANGER")

    @shield.protect(params={"coin": "symbol", "side": "direction", "size_usd": "notional"})
    def order(symbol, direction, notional):
        trades.append(1)

    with pytest.raises(TradeBlocked):
        order(symbol="BTC", direction="short", notional=1000)
    assert trades == [] and mock_apex.signed[0]["query"]["coin"] == "BTC"


def test_extract_from_an_order_object(mock_apex, shield):
    mock_apex.paid_response = safety(80, "LOW_RISK")

    class Order:
        coin, side, usd = "SOL", "short", 250

    @shield.protect(extract=lambda o: (o.coin, o.side, o.usd))
    def submit(o):
        return "ok"

    assert submit(Order()) == "ok"
    assert mock_apex.signed[0]["query"] == {"coin": "SOL", "side": "short", "size_usd": "250"}


def test_decorating_a_function_without_trade_params_fails_at_definition(shield):
    with pytest.raises(TypeError):
        @shield.protect
        def nope(x):
            pass


async def test_async_function(mock_apex, shield):
    mock_apex.paid_response = safety(20, "DANGER")
    ran = []

    @shield.protect
    async def place(coin, side, size_usd):
        ran.append(1)

    with pytest.raises(TradeBlocked):
        await place("ETH", "long", 5000)
    assert ran == []
    mock_apex.paid_response = safety(90, "LOW_RISK")

    @shield.protect
    async def place2(coin, side, size_usd):
        return "ok"

    assert await place2("ETH", "short", 5000) == "ok"


def test_apex_shield_one_liner(mock_apex, make_client, trades):
    mock_apex.paid_response = safety(30, "DANGER")

    @apex_shield(make_client())
    def place(coin, side, size_usd):
        trades.append(1)

    with pytest.raises(TradeBlocked):
        place("ETH", "long", 5000)
    assert trades == []


def test_trade_blocked_is_not_an_api_error():
    assert not issubclass(TradeBlocked, ApexError)


@pytest.mark.parametrize("bad", [0, 101, 40.0, True, "40"])
def test_block_below_validation(make_client, bad):
    with pytest.raises(ValueError):
        RiskShield(make_client(), block_below=bad)


def test_decision_is_immutable(mock_apex, shield):
    mock_apex.paid_response = safety(10, "DANGER")
    d = shield.check("ETH", "long", 5000)
    assert isinstance(d, ShieldDecision)
    with pytest.raises(Exception):
        d.allowed = True  # type: ignore[misc]
