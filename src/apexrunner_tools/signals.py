"""The four tools, defined once. Both framework adapters read from here."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .schemas import (
    CheckTradeSafetyInput,
    ExecutionWindowOptimizerInput,
    RiskAssessmentBundleInput,
    SlippageForecastInput,
)

_COST = (
    "Costs about $0.01 in USDC on Base, paid automatically from the agent's wallet. "
    "Repeating the same call while the data is still fresh returns the cached result at no cost."
)


@dataclass(frozen=True)
class SignalSpec:
    name: str
    slug: str
    input_model: type[BaseModel]
    description: str

    def to_query(self, args: BaseModel) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in args.model_dump().items():
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            out[key] = str(value)
        return out


RISK_ASSESSMENT_BUNDLE = SignalSpec(
    name="risk_assessment_bundle",
    slug="risk-assessment-bundle",
    input_model=RiskAssessmentBundleInput,
    description=(
        "Check crypto market risk before sizing or opening a position. One call returns 4 signals: "
        "liquidation cascade risk per coin (liquidation_pressure), the nearest liquidation clusters "
        "and which way price is being pulled (liquidation_magnet), overall market heat with a "
        "suggested action (portfolio_heat), and top-trader long/short positioning (whale_sentiment). "
        "Use it to avoid entering just as leverage is about to unwind. Takes no input. " + _COST
    ),
)

EXECUTION_WINDOW_OPTIMIZER = SignalSpec(
    name="execution_window_optimizer",
    slug="execution-window-optimizer",
    input_model=ExecutionWindowOptimizerInput,
    description=(
        "Decide whether to execute a crypto order now or wait. Scores the current window 0-100 "
        "from trading session, market regime, Fear & Greed and volatility, and returns a verdict "
        "(EXECUTE_NOW, GOOD_WINDOW, WAIT_15MIN or WAIT_SESSION), a recommended order type and the "
        "next favourable window. Input: coin (BTC, ETH, SOL, AVAX, LINK or XRP; default BTC). " + _COST
    ),
)

SLIPPAGE_FORECAST = SignalSpec(
    name="slippage_forecast",
    slug="slippage-forecast",
    input_model=SlippageForecastInput,
    description=(
        "Forecast the hidden market-impact cost of a crypto order before placing it. Returns "
        "expected slippage as a percentage and in USD for the given coin, size and side, plus a "
        "recommendation: EXECUTE_NOW, EXECUTE, SPLIT_ORDER (with a suggested tranche count) or "
        "USE_LIMIT. Use it for any order above a few hundred dollars. Inputs: coin (BTC, ETH, SOL, "
        "AVAX, LINK or XRP), size_usd (10 to 1,000,000), side (buy or sell). "
        "Example: coin=ETH, size_usd=5000, side=sell. " + _COST
    ),
)

CHECK_TRADE_SAFETY = SignalSpec(
    name="check_trade_safety",
    slug="check-trade-safety",
    input_model=CheckTradeSafetyInput,
    description=(
        "Pre-trade risk check for one specific trade. Scores a proposed coin, side and size 0-100 "
        "(higher = lower risk) from Hyperliquid liquidation clusters, top-trader crowding, funding "
        "and modelled slippage, and returns a verdict: LOW_RISK (65+), CAUTION (40-64) or DANGER "
        "(under 40), with the reason for each factor. The score is a heuristic, not a prediction "
        "or a guarantee. Inputs: coin (BTC, ETH or SOL), side (long or short), size_usd "
        "(10 to 10,000,000). Example: coin=ETH, side=long, size_usd=5000. Costs about $0.014 in "
        "USDC on Base, paid automatically from the agent's wallet; repeating the same check while "
        "it is still fresh returns the cached result at no cost."
    ),
)

SIGNALS: tuple[SignalSpec, ...] = (
    RISK_ASSESSMENT_BUNDLE,
    EXECUTION_WINDOW_OPTIMIZER,
    SLIPPAGE_FORECAST,
    CHECK_TRADE_SAFETY,
)

_BY_KEY = {key: spec for spec in SIGNALS for key in (spec.name, spec.slug)}


def get_spec(signal: str) -> SignalSpec:
    try:
        return _BY_KEY[signal]
    except KeyError:
        names = ", ".join(s.name for s in SIGNALS)
        raise ValueError(f"Unknown signal {signal!r}. Available: {names}") from None
