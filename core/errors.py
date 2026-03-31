class WalletError(Exception):
    """Base class for all wallet-related errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.code:
            return f"[{self.code}] {base}"
        return base


class WalletSecurityError(WalletError):
    """Raised when a security-sensitive operation fails
    (bad password, pickle attempt, pickle attempt, etc.)."""


class WalletValidationError(WalletError):
    """Raised when input validation fails (bad key format, empty message, etc.)."""
