# apexrunner-tools

Pre-trade crypto signals for **LangChain** and **CrewAI** agents. Each call is paid per use over [x402](https://x402.org) from the agent's own wallet: about $0.01 in USDC on Base mainnet. There is no API key, no account and no subscription.

| Tool | Input | Returns |
|---|---|---|
| `risk_assessment_bundle` | none | 4 signals in one call: liquidation cascade risk, nearest liquidation clusters, market heat, top-trader positioning |
| `execution_window_optimizer` | `coin` | 0–100 window score and a verdict: EXECUTE_NOW / GOOD_WINDOW / WAIT_15MIN / WAIT_SESSION, plus an order type |
| `slippage_forecast` | `coin`, `size_usd`, `side` | Expected slippage % and USD, and a recommendation: EXECUTE_NOW / EXECUTE / SPLIT_ORDER / USE_LIMIT |

Coins: BTC, ETH, SOL, AVAX, LINK, XRP.

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

LangChain turns these into a tool message the model can read. CrewAI tools return `{"success": false, "error": ...}`.

## License

MIT
