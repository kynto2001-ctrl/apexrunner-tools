"""Every failure the client raises. All subclass ApexError."""


class ApexError(Exception):
    """Base class for apexrunner-tools errors."""


class PaymentRefused(ApexError):
    """No payment option passed the local policy. Nothing was signed."""


class BudgetExceeded(PaymentRefused):
    """Signing this payment would exceed the client's spend budget. Nothing was signed."""


class PaymentRejected(ApexError):
    """A payment was signed but the server or facilitator refused it.

    Usually the wallet holds too little USDC on Base."""


class ApexUnavailable(ApexError):
    """APEX answered with a non-success status. x402 settles only on success."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ApexResponseError(ApexError):
    """APEX returned a success status with a body that is not a JSON object."""


class TradeBlocked(Exception):
    """RiskShield refused a trade. Deliberately NOT an ApexError: the API worked,
    the trade was judged too risky (or could not be checked, when failing closed).

    ``decision`` holds the full ShieldDecision. ``str(exc)`` is written so an LLM
    agent can read it and decide what to do next."""

    def __init__(self, decision: object):
        super().__init__(getattr(decision, "message", "Trade blocked by RiskShield."))
        self.decision = decision
