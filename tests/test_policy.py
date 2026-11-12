"""Fail-closed payment policy: positive control first, then every refusal.

A refusal must mean NOTHING WAS SIGNED: the mock server never receives a
payment signature, and the budget is untouched.
"""

import pytest

from apexrunner_tools import PaymentRefused
from apexrunner_tools.constants import APEX_PAY_TO, BASE_USDC, USER_AGENT

SLIP = dict(coin="ETH", size_usd=5000, side="sell")


async def _call(client, mode, signal="slippage_forecast", **kw):
    if mode == "sync":
        return client.call(signal, **kw)
    return await client.acall(signal, **kw)


@pytest.fixture(params=["sync", "async"])
def mode(request):
    return request.param


# ── positive control ──────────────────────────────────────────────────────────


async def test_real_challenge_is_paid_with_exact_terms(mock_apex, make_client, account, mode):
    client = make_client(cache=False)
    r = await _call(client, mode, **SLIP)

    assert r.data["query"] == {"coin": "ETH", "size_usd": "5000", "side": "sell"}
    assert r.amount_usdc == pytest.approx(0.007)
    assert r.transaction == "0x" + "ab" * 32
    assert r.payer == account.address

    [paid] = mock_apex.signed
    auth = paid["payload"]["payload"]["authorization"]
    assert auth["to"].lower() == APEX_PAY_TO.lower()
    assert auth["from"].lower() == account.address.lower()
    assert int(auth["value"]) == 7000
    accepted = paid["payload"]["accepted"]
    assert accepted["asset"].lower() == BASE_USDC.lower() and accepted["network"] == "eip155:8453"
    assert paid["ua"] == USER_AGENT
    assert client.budget.spent_usdc == pytest.approx(0.007)


async def test_picks_the_apex_option_when_a_bad_one_is_listed_first(mock_apex, make_client, mode):
    good = mock_apex.challenge["accepts"][0]
    bad = {**good, "payTo": "0x000000000000000000000000000000000000dEaD"}
    mock_apex.challenge["accepts"] = [bad, good]
    await _call(make_client(cache=False), mode, **SLIP)
    [paid] = mock_apex.signed
    assert paid["payload"]["payload"]["authorization"]["to"].lower() == APEX_PAY_TO.lower()


# ── refusals: nothing signed ──────────────────────────────────────────────────

REFUSALS = {
    "payTo swapped": lambda a: a.update(payTo="0x000000000000000000000000000000000000dEaD"),
    "amount over $0.02 cap": lambda a: a.update(amount="20001", maxAmountRequired="20001"),
    "amount zero": lambda a: a.update(amount="0", maxAmountRequired="0"),
    "amount not an integer": lambda a: a.update(amount="7000.5"),
    "network base-sepolia": lambda a: a.update(network="eip155:84532"),
    "network ethereum": lambda a: a.update(network="eip155:1"),
    "asset WETH": lambda a: a.update(asset="0x4200000000000000000000000000000000000006"),
    "scheme upto": lambda a: a.update(scheme="upto"),
}


@pytest.mark.parametrize("case", list(REFUSALS))
async def test_refused_terms_sign_nothing(mock_apex, make_client, mode, case):
    REFUSALS[case](mock_apex.challenge["accepts"][0])
    client = make_client(cache=False)
    with pytest.raises(PaymentRefused) as exc:
        await _call(client, mode, **SLIP)
    assert "Nothing was signed" in str(exc.value)
    assert mock_apex.signed == []
    assert len(mock_apex.requests) == 1  # only the unpaid probe
    assert client.budget.spent_usdc == 0


async def test_v1_challenge_is_refused(mock_apex, make_client, mode):
    a = mock_apex.challenge["accepts"][0]
    mock_apex.challenge = {
        "x402Version": 1,
        "error": "Payment required",
        "accepts": [{
            "scheme": "exact", "network": "base", "maxAmountRequired": "7000",
            "resource": mock_apex.base_url, "description": "", "mimeType": "application/json",
            "payTo": a["payTo"], "maxTimeoutSeconds": 300, "asset": a["asset"], "extra": a["extra"],
        }],
    }
    client = make_client(cache=False)
    with pytest.raises(PaymentRefused):
        await _call(client, mode, **SLIP)
    assert mock_apex.signed == []
    assert client.budget.spent_usdc == 0


async def test_lower_cap_refuses_current_price(mock_apex, make_client, mode):
    client = make_client(cache=False, max_payment_usdc=0.005)  # price is 0.007
    with pytest.raises(PaymentRefused):
        await _call(client, mode, **SLIP)
    assert mock_apex.signed == []


@pytest.mark.parametrize("cap", [0, -1, 1.01, 50])
def test_cap_must_be_in_range(make_client, cap):
    with pytest.raises(ValueError):
        make_client(max_payment_usdc=cap)


# ── unit level: each check on its own, independent of the SDK's own filtering ──

from types import SimpleNamespace  # noqa: E402

from apexrunner_tools.payment import apex_only_policy, is_apex_requirement  # noqa: E402

GOOD = dict(scheme="exact", network="eip155:8453", asset=BASE_USDC, pay_to=APEX_PAY_TO, amount="7000")


@pytest.mark.parametrize(
    "field,value",
    [
        ("scheme", "upto"),
        ("network", "eip155:84532"),
        ("network", "base"),
        ("asset", "0x4200000000000000000000000000000000000006"),
        ("pay_to", "0x000000000000000000000000000000000000dEaD"),
        ("amount", "20001"),
        ("amount", "0"),
        ("amount", "-1"),
        ("amount", "7e3"),
        ("amount", ""),
    ],
)
def test_each_requirement_field_is_checked(field, value):
    assert is_apex_requirement(SimpleNamespace(**GOOD), 20_000)
    assert not is_apex_requirement(SimpleNamespace(**{**GOOD, field: value}), 20_000)


def test_address_comparison_is_case_insensitive():
    req = SimpleNamespace(**{**GOOD, "pay_to": APEX_PAY_TO.lower(), "asset": BASE_USDC.upper().replace("0X", "0x")})
    assert is_apex_requirement(req, 20_000)


def test_policy_drops_everything_for_v1_even_if_fields_look_right():
    policy = apex_only_policy(20_000)
    assert policy(2, [SimpleNamespace(**GOOD)]) != []
    assert policy(1, [SimpleNamespace(**GOOD)]) == []
