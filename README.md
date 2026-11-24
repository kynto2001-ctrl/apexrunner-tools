# apexrunner-tools

**RiskShield is a pre-trade gate that blocks your trading agent's riskiest orders, such as a long opened just above a wall of liquidations, before they reach the exchange.** Put one decorator on the function that places orders. Every trade is then scored 0–100 against live market structure before it runs, and a trade scored DANGER never reaches your exchange. It covers BTC, ETH and SOL. Each check costs $0.014 in USDC, paid per call over [x402](https://x402.org). There is no API key, no account and no subscription.

```python
import os
from apexrunner_tools import ApexClient, RiskShield

shield = RiskShield(ApexClient(private_key=os.environ["AGENT_WALLET_KEY"]))

@shield.protect
def place_order(coin: str, side: str, size_usd: float) -> dict:
    return exchange.market_order(coin, side, size_usd)   # your code, unchanged

place_order("ETH", "long", 5000)   # raises TradeBlocked if the trade scores DANGER
```

### Why an agent needs a gate in code

An LLM picks a coin, a side and a size from text. It can't see that a cluster of leveraged positions would be liquidated by a small move past the mark price, that the top traders are already crowded on the same side, or that its order is too big for the book. A prompt that says "check risk first" is a suggestion the model can skip. RiskShield wraps the order function itself, so the check can't be skipped. A failed check (an unfunded wallet, an exhausted budget, an outage) **blocks** the trade by default rather than letting it through.

What it checks, and what it doesn't: the score combines Hyperliquid liquidation clusters near the mark price, top-trader crowding on your side, the funding your side pays, and modelled slippage for your size. It is a heuristic and is not yet calibrated against outcomes. It does not manage open positions or set stops. → [Full RiskShield docs](#riskshield-block-dangerous-trades-before-they-execute) · [Fund the wallet first](#fund-your-agents-wallet-do-this-first)

## What else is in the box

The same client also ships pre-trade signal tools for **LangChain** and **CrewAI** agents, at about $0.01 per call:

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

## Fund your agent's wallet (do this first)

Every call is paid from the agent's own wallet. **With an empty wallet, nothing works, and RiskShield blocks every trade.** That is deliberate: it fails closed. The tool is not broken.

1. **Create a wallet just for the agent.** Don't reuse a wallet that holds anything else:
   ```bash
   python -c "from eth_account import Account; a = Account.create(); print(a.address, a.key.hex())"
   export AGENT_WALLET_KEY=<the key printed above>   # keep it out of git
   ```
2. **Send it USDC on Base mainnet.** It needs USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) **on the Base network**, not Ethereum, Arbitrum or any other chain. When you withdraw from an exchange, choose *Base* as the network. You don't need any ETH because the facilitator pays the gas.
3. **Check that it arrived:** `https://basescan.org/token/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913?a=<your address>`

**How much:** $1 is enough to start.

| Call | Price | $1 covers |
|---|---|---|
| `check_trade_safety` (RiskShield) | $0.014 | about 70 checks |
| other tools | about $0.01 | about 100 calls |

You pay only for calls that succeed. A repeated identical call is served from cache at no cost while its data is still valid. The client also stops itself at `budget_usdc` (default $1.00), whatever the balance.

**If the wallet is empty**, nothing is charged and no order is placed. RiskShield raises `TradeBlocked` with:

```
BLOCKED: long ETH $500 could not be risk-checked: PaymentRejected: Payment was rejected.
Check that wallet 0x54586281dFbcdF3e496B0A8dd58d3512B603b0B1 holds USDC on Base mainnet (about $0.01 per call).
```

Here `e.decision.error` is set and `e.decision.score` is `None`. That combination means the check never ran. A real DANGER result has a score.

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

Nothing stops an LLM agent from opening a position right next to a liquidation cluster. A prompt can tell an agent to check risk first, but the model can skip a prompt. RiskShield can't be skipped: it wraps the function that places the order, so every trade is scored by `check_trade_safety` before it runs, and a trade in the DANGER band never reaches your exchange code. $0.014 per check, paid from the agent's own USDC wallet on Base ([fund it first](#fund-your-agents-wallet-do-this-first)). No API key.

**Quickstart:** `pip install apexrunner-tools`, then:

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

**Cost:** one check is $0.014. An identical check (same coin, side and size) within the response's validity window (120 s) is served from cache at no cost. The default $1.00 client budget covers about 70 checks. When it is spent, trades are blocked (fail closed) until you raise the budget. The wallet itself needs USDC on Base. If every trade is blocked with no score, see [Fund your agent's wallet](#fund-your-agents-wallet-do-this-first).

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

## Local Risk Limits

Your own hard caps, enforced by the shield **before** the paid check: a max size per trade, a max USD traded per rolling 24 hours, and a portfolio auto-stop. They're free to evaluate. A trade that breaks one is refused without paying for a `check_trade_safety` call, and it never reaches your exchange code.

> **These are client-side limits, not tamper-proof ones.** They run inside your agent's own process. Code that can edit `apex_risk.toml` or delete the state file can remove them. Venue-side controls such as Coinbase's Agent Controls run on the exchange's servers, where your agent can't touch them. Use those wherever a venue offers them, and treat these limits as the layer that also covers venues that don't.

**1. Enter your limits in `apex_risk.toml`.** Nothing is read from your exchange: Coinbase, for one, has no API that exposes Agent Controls settings. If you've set limits there, copy the numbers here yourself.

```toml
[limits]
max_trade_usd      = 100     # largest single order, USD
max_24h_volume_usd = 1000    # most USD traded in any rolling 24 h
portfolio_stop_pct = 5       # or "5%": stop when equity is down more than 5% from baseline

[state]
path = "~/.apexrunner/risk_ledger.sqlite"   # default; relative paths are relative to this file
```

Any limit can be left out. Environment variables override the file: `APEX_MAX_TRADE_USD`, `APEX_MAX_24H_VOLUME_USD`, `APEX_PORTFOLIO_STOP_PCT`, `APEX_RISK_STATE`, and `APEX_RISK_CONFIG` for the file's location. Bad configuration is an error at startup, never a silently skipped limit: an unknown key, a non-numeric or non-positive value, a stop of 100% or more, or a config that sets no limit at all.

**2. Pass the limits to the shield.**

```python
import os
from apexrunner_tools import ApexClient, RiskShield, LocalLimitBlocked, TradeBlocked

shield = RiskShield(
    ApexClient(private_key=os.environ["AGENT_WALLET_KEY"]),
    limits="apex_risk.toml",                                    # or limits=load_limits()
    equity_fn=lambda: kraken_equity() + hyperliquid_equity(),   # required for portfolio_stop_pct
)

@shield.protect
def place_order(coin: str, side: str, size_usd: float) -> dict: ...

try:
    place_order("ETH", "long", 250)
except LocalLimitBlocked as e:
    print(e)   # Blocked by local risk limits: long ETH $250: $250.00 exceeds the $100.00 max trade size
except TradeBlocked as e:
    print(e)   # passed your limits, refused by the safety score
```

`LocalLimitBlocked` subclasses `TradeBlocked`, so an existing `except TradeBlocked` handler catches both. `e.decision.limit` names the limit that fired.

**3. The limits apply across every venue that uses the shield.** The 24 h volume total and the stop live in one SQLite file. Every shield that points at the same `state_path`, in any process and for any venue, shares them. Decorate your Kraken order function and your Hyperliquid order function with shields that use the same file, and $700 on Kraken leaves $300 for Hyperliquid. Concurrent agents can't both spend the same headroom: each reservation is atomic. Only trades that go through the shield are counted. Orders placed by hand, from an exchange UI, or by undecorated code are invisible to it. If you place orders after a manual `shield.guard(...)`, count them with `shield.record_volume(size_usd)`.

**How each limit behaves:**

| Limit | Passes at the boundary? | Notes |
|---|---|---|
| `max_trade_usd` | Yes: a $100 trade passes a $100 limit | Checked first, no state |
| `max_24h_volume_usd` | Yes: reaching exactly $1,000 passes | Rolling window. A trade counts from the moment it's allowed. If `check_trade_safety` then blocks it, it's released. If your order function raises, it **stays counted**, since the order may have reached the venue. |
| `portfolio_stop_pct` | Yes: exactly -5.00% passes, -5.01% trips | Arms on the first trade with the equity at that moment as baseline. Once tripped, every trade is blocked, even if equity recovers or the agent restarts, until you call `shield.reset_stop()`, which re-arms at current equity. Call `reset_stop()` after deposits or withdrawals too. A tripped stop doesn't cancel open orders or close positions. |

**Fails closed, always.** If the state file can't be read (corrupted, written by something else, locked past 10 s, deleted while running), if `equity_fn` raises, or if it returns anything other than a finite, non-negative number, the trade is blocked. `fail_open=True` does not apply to local limits; it only governs the paid check. `shield.local_status()` shows volume used and remaining and the stop's baseline and state. `shield.check(...)` evaluates the limits read-only and never adds to the 24 h volume. Only `@shield.protect` (and `record_volume`) do.

`equity_fn` must be a plain function. In async code it runs in a worker thread, along with the state-file reads.

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every trade `BLOCKED ... could not be risk-checked: PaymentRejected` | The wallet has no USDC on Base, or not enough | [Fund it](#fund-your-agents-wallet-do-this-first). Check the balance on basescan.org, not on the Ethereum explorer |
| You sent USDC but it is still rejected | It went to a different chain (Ethereum, Arbitrum, …) | Send USDC to the same address, with *Base* as the network |
| `BLOCKED ... could not be risk-checked: BudgetExceeded` | The client's own `budget_usdc` is spent, whatever the balance | Make a new client, or pass a larger `budget_usdc=` |
| `BLOCKED ... could not be risk-checked: ...invalid or unsupported trade parameters` | Coin not BTC/ETH/SOL, or size outside $10–$10M | Change the trade. Other coins can't be checked, and are blocked unless `fail_open=True` |
| `BLOCKED: ... scored 31/100 (DANGER ...)` | Working as intended: the check ran and the trade scored DANGER | Resize, flip or skip. `e.decision.reasons` says why |

## Errors

All errors subclass `ApexError`:

| Error | Meaning | Charged? |
|---|---|---|
| `PaymentRefused` | The payment request didn't match the pinned terms | No, nothing was signed |
| `BudgetExceeded` | The budget is spent | No, nothing was signed |
| `PaymentRejected` | A payment was signed but refused, usually too little USDC | No settlement |
| `ApexUnavailable` | The server returned a non-2xx status | x402 settles only on success |
| `ApexResponseError` | The server returned a 2xx with an unreadable body | Possibly; report it |

`TradeBlocked` (RiskShield) is separate and is **not** an `ApexError`: the API worked, and the trade was refused. `e.decision` holds the full `ShieldDecision`. `LocalLimitBlocked` is a `TradeBlocked` raised by [local risk limits](#local-risk-limits) before any paid call. `LedgerError` means the limits' state file is unusable; the shield turns it into a blocked trade, so you only see it from `shield.local_status()`.

LangChain turns these into a tool message the model can read. CrewAI tools return `{"success": false, "error": ...}`.

## License

MIT
