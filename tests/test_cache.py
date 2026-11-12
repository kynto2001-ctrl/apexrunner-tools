"""Expiring cache: free repeats while data is valid; never stale."""

import pytest

SLIP = dict(coin="ETH", size_usd=5000, side="sell")


def _body(**extra):
    return lambda p, q: (200, {"recommendation": "EXECUTE", **extra}, {})


def test_repeat_within_validity_is_free(mock_apex, make_client, clock):
    mock_apex.paid_response = _body(valid_for_seconds=120, data_age_seconds=0)
    client = make_client()
    first = client.call("slippage_forecast", **SLIP)
    clock.now += 119
    second = client.call("slippage_forecast", **SLIP)
    assert (first.cached, second.cached) == (False, True)
    assert second.amount_usdc == 0.0 and second.data == first.data
    assert len(mock_apex.requests) == 2  # one probe + one paid; the repeat never left the process
    assert client.budget.spent_usdc == pytest.approx(0.007)


def test_expired_entry_is_paid_for_again(mock_apex, make_client, clock):
    mock_apex.paid_response = _body(valid_for_seconds=120)
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    clock.now += 120
    assert client.call("slippage_forecast", **SLIP).cached is False
    assert len(mock_apex.signed) == 2


def test_data_age_shortens_validity(mock_apex, make_client, clock):
    mock_apex.paid_response = _body(valid_for_seconds=120, data_age_seconds=100)
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    clock.now += 19
    assert client.call("slippage_forecast", **SLIP).cached is True
    clock.now += 2  # 21s total > 20s left
    assert client.call("slippage_forecast", **SLIP).cached is False


def test_cache_is_capped_at_300s(mock_apex, make_client, clock):
    mock_apex.paid_response = _body(valid_for_seconds=3600)
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    clock.now += 300
    assert client.call("slippage_forecast", **SLIP).cached is False


@pytest.mark.parametrize(
    "body",
    [
        {},  # no validity given
        {"valid_for_seconds": 120, "stale": True},
        {"valid_for_seconds": 0},
        {"valid_for_seconds": "120"},
        {"valid_for_seconds": True},
    ],
)
def test_uncacheable_responses_are_not_cached(mock_apex, make_client, body):
    mock_apex.paid_response = lambda p, q: (200, dict(body), {})
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    assert client.call("slippage_forecast", **SLIP).cached is False
    assert len(mock_apex.signed) == 2


def test_different_arguments_are_different_entries(mock_apex, make_client):
    mock_apex.paid_response = _body(valid_for_seconds=120)
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    client.call("slippage_forecast", coin="ETH", size_usd=5000, side="buy")
    assert len(mock_apex.signed) == 2


def test_equivalent_arguments_share_an_entry(mock_apex, make_client):
    mock_apex.paid_response = _body(valid_for_seconds=120)
    client = make_client()
    client.call("slippage_forecast", coin="ETH", size_usd=5000, side="sell")
    assert client.call("slippage-forecast", side="sell", size_usd=5000.0, coin="ETH").cached is True


def test_failed_calls_are_not_cached(mock_apex, make_client):
    from apexrunner_tools import ApexUnavailable

    responses = iter([(503, {"error": "x"}, {}), (200, {"valid_for_seconds": 120}, {})])
    mock_apex.paid_response = lambda p, q: next(responses)
    client = make_client()
    with pytest.raises(ApexUnavailable):
        client.call("slippage_forecast", **SLIP)
    assert client.call("slippage_forecast", **SLIP).cached is False


def test_cache_off_always_pays(mock_apex, make_client):
    mock_apex.paid_response = _body(valid_for_seconds=120)
    client = make_client(cache=False)
    client.call("slippage_forecast", **SLIP)
    client.call("slippage_forecast", **SLIP)
    assert len(mock_apex.signed) == 2


async def test_async_uses_the_same_cache(mock_apex, make_client):
    mock_apex.paid_response = _body(valid_for_seconds=120)
    client = make_client()
    client.call("slippage_forecast", **SLIP)
    assert (await client.acall("slippage_forecast", **SLIP)).cached is True
