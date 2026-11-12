"""LangChain and CrewAI adapters, and the wallet/input plumbing behind them."""

import json

import pytest
from pydantic import ValidationError

from apexrunner_tools import ApexClient
from apexrunner_tools.crewai import get_apex_tools as crew_tools
from apexrunner_tools.langchain import get_apex_tools as lc_tools

NAMES = ["risk_assessment_bundle", "execution_window_optimizer", "slippage_forecast"]


def _tool_call(name, args):
    return {"name": name, "args": args, "id": "call_1", "type": "tool_call"}


# ── LangChain ────────────────────────────────────────────────────────────────


def test_langchain_tools_and_schemas(make_client):
    tools = lc_tools(make_client())
    assert [t.name for t in tools] == NAMES
    slip = tools[2].tool_call_schema.model_json_schema()["properties"]
    assert slip["size_usd"]["minimum"] == 10 and slip["size_usd"]["maximum"] == 1_000_000
    assert slip["coin"]["enum"] == ["BTC", "ETH", "SOL", "AVAX", "LINK", "XRP"]
    assert slip["side"]["enum"] == ["buy", "sell"]
    assert tools[0].tool_call_schema.model_json_schema().get("properties", {}) == {}
    assert all("USDC on Base" in t.description for t in tools)


def test_langchain_tools_share_one_client(make_client):
    tools = lc_tools(make_client())
    assert len({id(t.client) for t in tools}) == 1


def test_langchain_returns_content_and_artifact(mock_apex, make_client):
    tool = lc_tools(make_client(cache=False))[2]
    msg = tool.invoke(_tool_call("slippage_forecast", {"coin": "ETH", "size_usd": 5000, "side": "sell"}))
    content = json.loads(msg.content)
    assert content["signal"] == "slippage_forecast"
    assert content["payment"]["amount_usdc"] == pytest.approx(0.007)
    assert msg.artifact["payment"]["transaction"] == "0x" + "ab" * 32
    assert msg.artifact["query"] == {"coin": "ETH", "size_usd": "5000", "side": "sell"}


async def test_langchain_async(mock_apex, make_client):
    tool = lc_tools(make_client(cache=False))[1]
    msg = await tool.ainvoke(_tool_call("execution_window_optimizer", {"coin": "SOL"}))
    assert json.loads(msg.content)["data"]["query"] == {"coin": "SOL"}


def test_langchain_no_arg_tool(mock_apex, make_client):
    msg = lc_tools(make_client(cache=False))[0].invoke(_tool_call("risk_assessment_bundle", {}))
    assert json.loads(msg.content)["data"]["query"] == {}


def test_langchain_failure_becomes_readable_tool_message(mock_apex, make_client):
    mock_apex.challenge["accepts"][0]["payTo"] = "0x000000000000000000000000000000000000dEaD"
    msg = lc_tools(make_client())[1].invoke(_tool_call("execution_window_optimizer", {"coin": "BTC"}))
    assert msg.status == "error"
    assert "Nothing was signed" in msg.content
    assert mock_apex.signed == []


def test_langchain_invalid_args_never_reach_the_network(mock_apex, make_client):
    tool = lc_tools(make_client())[2]
    with pytest.raises(ValidationError):
        tool.invoke(_tool_call("slippage_forecast", {"coin": "ETH", "size_usd": 5, "side": "sell"}))
    assert mock_apex.requests == []


def test_get_tools_rejects_client_and_kwargs(make_client):
    with pytest.raises(ValueError):
        lc_tools(make_client(), budget_usdc=5)
    with pytest.raises(ValueError):
        crew_tools(make_client(), budget_usdc=5)


# ── CrewAI ───────────────────────────────────────────────────────────────────


def test_crewai_tools(mock_apex, make_client):
    tools = crew_tools(make_client(cache=False))
    assert [t.name for t in tools] == NAMES
    assert all(t.cache_function({}, "x") is False for t in tools)
    out = json.loads(tools[2].run(coin="ETH", size_usd=5000, side="sell"))
    assert out["payment"]["transaction"] == "0x" + "ab" * 32


def test_crewai_failure_is_returned_not_raised(mock_apex, make_client):
    mock_apex.challenge["accepts"][0]["amount"] = "50000"
    out = json.loads(crew_tools(make_client())[0].run())
    assert out["success"] is False and "Nothing was signed" in out["error"]
    assert mock_apex.signed == []


# ── wallet + inputs ──────────────────────────────────────────────────────────


def test_private_key_never_leaks_into_strings_or_serialisation(mock_apex, make_client, account):
    """The signer must hold the key to sign; it must never surface as text."""
    key = account.key.hex().removeprefix("0x")
    client = ApexClient(private_key="0x" + key, base_url=mock_apex.base_url)
    assert client.address == account.address

    def strings(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from strings(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from strings(v, depth + 1)
        elif hasattr(obj, "__dict__") and not isinstance(obj, type):
            for v in vars(obj).values():
                yield from strings(v, depth + 1)

    assert all(key not in s.lower() for s in strings(client))
    assert key not in repr(client).lower()
    for tool in lc_tools(client) + crew_tools(client):
        assert key not in repr(tool).lower()
        assert key not in json.dumps(tool.model_dump(mode="python", exclude={"args_schema"}), default=str).lower()
    client.close()


@pytest.mark.parametrize("kw", [{}, {"private_key": "0x" + "11" * 32, "signer": object()}])
def test_exactly_one_wallet_source(kw):
    with pytest.raises(ValueError):
        ApexClient(**kw)


def test_custom_signer_must_look_like_a_signer():
    with pytest.raises(TypeError):
        ApexClient(signer=object())


def test_custom_signer_is_used(mock_apex, account):
    from x402.mechanisms.evm.signers import EthAccountSigner

    inner = EthAccountSigner(account)

    class KmsLikeSigner:
        calls = 0

        @property
        def address(self):
            return inner.address

        def sign_typed_data(self, *a, **k):
            KmsLikeSigner.calls += 1
            return inner.sign_typed_data(*a, **k)

    with ApexClient(signer=KmsLikeSigner(), base_url=mock_apex.base_url, cache=False) as c:
        c.call("risk_assessment_bundle")
    assert KmsLikeSigner.calls == 1


def test_unknown_signal(make_client):
    with pytest.raises(ValueError, match="Available"):
        make_client().call("market_tick")
