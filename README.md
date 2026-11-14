# apexrunner-tools

Pre-trade crypto signals for **LangChain** and **CrewAI** agents, plus **RiskShield**, a pre-trade gate that blocks high-risk trades in code before they reach your exchange. Each call is paid per use over [x402](https://x402.org) from the agent's own wallet: about $0.01 in USDC on Base mainnet ($0.014 for `check_trade_safety`). There is no API key, no account and no subscription.

| Tool | Input | Returns |
|---|---|---|
| `risk_assessment_bundle` | none | 4 signals in one call: liquidation cascade risk, nearest liquidation clusters, market heat, top-trader positioning |
| `execution_window_optimizer` | `coin` | 0–100 window score and a verdict: EXECUTE_NOW / GOOD_WINDOW / WAIT_15MIN / WAIT_SESSION, plus an order type |
| `slippage_forecast` | `coin`, `size_usd`, `side` | Expected slippage % and USD, and a recommendation: EXECUTE_NOW / EXECUTE / SPLIT_ORDER / USE_LIMIT |
| `check_trade_safety` | `coin`, `side`, `size_usd` (all required) | 0–100 safety score for one specific trade and a verdict: LOW_RISK (65+) / CAUTION (40–64) / DANGER (<40), with the reason for each factor |

Coins: BTC, ETH, SOL, AVAX, LINK, XRP. `check_trade_safety` covers BTC, ETH and SOL, `long`/`short` (`buy`/`sell` accepted), $10 to $10,000,000.

## Install

```bash
pip install "apexrunner-tools[langchain]"   # or [crewai]
```

## LangChain

```python
import os
from langchain.agents import create_agent
from apexrunner_tools.langchain import get_apex_tools

tools = get_apex_tools(private_key=os.environ["AGENT_WALLET_KEY"])  # $1.00 budget by default
agent = create_agent("openai:gpt-4.1-mini", tools=tools)
```

Each tool returns `content_and_artifact`. The model sees compact JSON. The `ToolMessage.artifact` holds the full data and the payment receipt (amount and transaction hash).

## CrewAI

```python
import os
from crewai import Agent
from apexrunner_tools.crewai import get_apex_tools

trader = Agent(
    role="Execution trader",
    goal="Place orders at the lowest total cost",
    backstory="Checks risk, timing and slippage before every order.",
    tools=get_apex_tools(private_key=os.environ["AGENT_WALLET_KEY"]),
)
```

## Without a framework

```python
from apexrunner_tools import ApexClient

with ApexClient(private_key=os.environ["AGENT_WALLET_KEY"]) as apex:
    r = apex.call("slippage_forecast", coin="ETH", size_usd=5000, side="sell")
    print(r.data["recommendation"], r.amount_usdc, r.transaction)
    print(apex.budget.spent_usdc, apex.budget.remaining_usdc)
```

`acall()` is the async version.

## RiskShield: block dangerous trades before they execute

A prompt can tell an agent to check risk first. RiskShield makes it impossible to skip: it wraps the function that places the order, so every trade is scored by `check_trade_safety` before it runs, and a trade in the DANGER band never reaches your exchange code.

```python
import os
from apexrunner_tools import ApexClient, RiskShield, TradeBlocked

shield = RiskShield(ApexClient(private_key=os.environ["AGENT_WALLET_KEY"]))

@shield.protect
def place_order(coin: str, side: str, size_usd: float) -> dict:
    return exchange.market_order(coin, side, size_usd)   # your code, unchanged

try:
    place_order("ETH", "long", 5000)
except TradeBlocked as e:
    print(e)              # BLOCKED: long ETH $5,000 scored 31/100 (DANGER; blocks under 40). ...
    e.decision.reasons    # one line per factor, highest risk first
```

| Option | Default | Effect |
|---|---|---|
| `block_below` | `40` | Block when the score is below this. 40 blocks DANGER only; `65` also blocks CAUTION. A DANGER verdict always blocks. |
| `fail_open` | `False` | If the check can't be completed (APEX unavailable, budget spent, unsupported coin, unreadable response), the trade is **blocked**. Set `True` to let it through instead; the decision still records why. |
| `on_decision` | `None` | Callback that receives every `ShieldDecision` (score, verdict, reasons, error, receipt). Use it to log or alert. |

Your function's own `coin`, `side` and `size_usd` arguments are used. If they're named differently, map them with `@shield.protect(params={"coin": "symbol", "side": "direction", "size_usd": "notional"})`. For an order object, use `@shield.protect(extract=lambda order: (order.coin, order.side, order.usd))`. Async functions work the same way. To score without wrapping, call `shield.check(coin, side, size_usd)`, which returns a `ShieldDecision` and never raises, or `shield.guard(...)`, which raises `TradeBlocked`. For one-off use, `@apex_shield(private_key=...)` creates the client and the shield in one line.

**Cost:** one check is $0.014. An identical check (same coin, side and size) within the response's validity window (120 s) is served from cache at no cost. The default $1.00 client budget covers about 70 checks. When it is spent, trades are blocked (fail closed) until you raise the budget.

**In an agent:** let the LLM decide what to trade, and put the shield on the tool that executes. `TradeBlocked` is not an `ApexError` and is not caught by the tools' error handling, so catch it in your order tool and return `str(e)` to the model. The message explains why, and the agent can resize, flip or skip:

```python
from langchain_core.tools import tool

@tool
def execute_trade(coin: str, side: str, size_usd: float) -> str:
    """Place a market order. It is risk-checked first and may be refused."""
    try:
        return str(place_order(coin, side, size_usd))
    except TradeBlocked as e:
        return str(e)
```

**What the score is, and isn't:** a heuristic computed from live market structure: Hyperliquid liquidation clusters near the mark price, top-trader crowding on your side, funding paid by your side, and modelled slippage for your size. The weights are not yet calibrated against outcomes (`method.calibrated: false` in every response). A trade that passes is not "safe", and none of this is investment advice. RiskShield is a guard against walking into obvious structural risk, not a strategy.

## Wallet

Supply exactly one of these:

- `private_key="0x…"`. It is turned into an `eth_account` signer, which holds the key only so it can sign. The key never appears in any attribute, `repr()` or a tool's serialised form.
- `account=LocalAccount`, from `eth_account`.
- `signer=...`, any object with an `address` and `sign_typed_data(...)`. Use this for KMS, HSM or hosted wallets.

The wallet needs **USDC on Base mainnet**, and no ETH. Payments are signed USDC authorizations (EIP-3009), and the facilitator pays the gas. One dollar covers about 100 calls.

## Safeguards

| Safeguard | What it does |
|---|---|
| **Pinned payment terms** | Before anything is signed, every payment option is checked: it must be x402 v2 `exact` USDC on Base, payable to APEX Runner's pinned address. Anything else is refused and **nothing is signed** (`PaymentRefused`). This holds even if the server, DNS or a proxy is compromised. |
| **Per-call cap** | `max_payment_usdc`, default **$0.02** and never above $1.00. A higher price is refused before signing. |
| **Spend budget** | `budget_usdc`, default **$1.00** per client. It is checked at the moment of signing, every signature counts (including the SDK's automatic re-sign on recovery), and it is safe across threads. When the budget is spent, calls raise `BudgetExceeded` and nothing is signed. A signed payment counts as spent even if the call then fails: the budget never assumes a payment was not settled. |
| **Expiring cache** | Repeating an identical call while its data is still valid (the response's `valid_for_seconds` minus its age, at most 300 s) returns the cached result at no cost. Stale data is never cached. CrewAI's own cache has no expiry, so it is switched off for these tools. |

## Errors

All errors subclass `ApexError`:

| Error | Meaning | Charged? |
|---|---|---|
| `PaymentRefused` | The payment request didn't match the pinned terms | No, nothing was signed |
| `BudgetExceeded` | The budget is spent | No, nothing was signed |
| `PaymentRejected` | A payment was signed but refused, usually too little USDC | No settlement |
| `ApexUnavailable` | The server returned a non-2xx status | x402 settles only on success |
| `ApexResponseError` | The server returned a 2xx with an unreadable body | Possibly; report it |

`TradeBlocked` (RiskShield) is separate and is **not** an `ApexError`: the API worked, and the trade was refused. `e.decision` holds the full `ShieldDecision`.

LangChain turns these into a tool message the model can read. CrewAI tools return `{"success": false, "error": ...}`.

## License

MIT
