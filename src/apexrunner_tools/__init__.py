"""APEX Runner pre-trade crypto signals for LangChain and CrewAI agents, paid per call over x402,
plus RiskShield (``apexrunner_tools.shield``), a fail-closed pre-trade gate on check_trade_safety
with optional local risk limits (``apexrunner_tools.limits``, apex_risk.toml).

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
    LocalLimitBlocked,
    PaymentRefused,
    PaymentRejected,
    TradeBlocked,
)
from .ledger import LedgerError
from .limits import RiskLimits, load_limits
from .shield import RiskShield, ShieldDecision, apex_shield
from .signals import SIGNALS, SignalSpec

__all__ = [
    "ApexClient",
    "ApexResult",
    "ApexError",
    "ApexResponseError",
    "ApexUnavailable",
    "BudgetExceeded",
    "LedgerError",
    "LocalLimitBlocked",
    "PaymentRefused",
    "PaymentRejected",
    "RiskLimits",
    "RiskShield",
    "ShieldDecision",
    "TradeBlocked",
    "apex_shield",
    "load_limits",
    "SIGNALS",
    "SignalSpec",
    "__version__",
]
