"""APEX Runner pre-trade crypto signals for LangChain and CrewAI agents, paid per call over x402,
plus RiskShield (``apexrunner_tools.shield``), a fail-closed pre-trade gate on check_trade_safety.

Framework adapters live in ``apexrunner_tools.langchain`` and ``apexrunner_tools.crewai``
so that importing the core never requires either framework.
"""

from .client import ApexClient, ApexResult
from .constants import __version__
from .errors import (
    ApexError,
    ApexResponseError,
    ApexUnavailable,
    BudgetExceeded,
    PaymentRefused,
    PaymentRejected,
    TradeBlocked,
)
from .shield import RiskShield, ShieldDecision, apex_shield
from .signals import SIGNALS, SignalSpec

__all__ = [
    "ApexClient",
    "ApexResult",
    "ApexError",
    "ApexResponseError",
    "ApexUnavailable",
    "BudgetExceeded",
    "PaymentRefused",
    "PaymentRejected",
    "RiskShield",
    "ShieldDecision",
    "TradeBlocked",
    "apex_shield",
    "SIGNALS",
    "SignalSpec",
    "__version__",
]
