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
