"""Tool inputs, shared by the LangChain and CrewAI adapters.

Bounds mirror the server's own validation (apex-signals PARAM_SPECS).
"""

from typing import Literal

from pydantic import BaseModel, Field

Coin = Literal["BTC", "ETH", "SOL", "AVAX", "LINK", "XRP"]


class RiskAssessmentBundleInput(BaseModel):
    """No input: a market-wide risk snapshot."""


class ExecutionWindowOptimizerInput(BaseModel):
    coin: Coin = Field(default="BTC", description="Asset symbol to score the execution window for.")


class SlippageForecastInput(BaseModel):
    coin: Coin = Field(default="BTC", description="Asset symbol of the order.")
    size_usd: float = Field(
        ge=10, le=1_000_000, description="Order notional in USD, between 10 and 1,000,000. Example: 5000."
    )
    side: Literal["buy", "sell"] = Field(default="buy", description="Order side.")
