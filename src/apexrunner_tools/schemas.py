"""Tool inputs, shared by the LangChain and CrewAI adapters.

Bounds mirror the server's own validation (apex-signals PARAM_SPECS).
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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


class CheckTradeSafetyInput(BaseModel):
    coin: Literal["BTC", "ETH", "SOL"] = Field(description="Asset of the proposed trade. BTC, ETH or SOL.")
    side: Literal["long", "short"] = Field(
        description="Direction of the proposed trade: long or short (buy/sell are accepted as aliases)."
    )
    size_usd: float = Field(
        ge=10, le=10_000_000, description="Trade notional in USD, between 10 and 10,000,000. Example: 5000."
    )

    @field_validator("coin", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("side", mode="before")
    @classmethod
    def _side_alias(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            return {"buy": "long", "sell": "short"}.get(v, v)
        return v
