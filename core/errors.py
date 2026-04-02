"""Exception hierarchy for wallet validation, security, and token math.

Subclasses of :class:`WalletError` carry optional ``code`` and ``details`` for APIs
that need structured error responses.
"""

from __future__ import annotations


class WalletError(Exception):
    """Base class for wallet-related failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        """
        Args:
            message: Human-readable description.
            code: Optional machine-readable code (e.g. ``INVALID_KEY``).
            details: Optional extra context (keep small; avoid secrets).
        """
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.code:
            return f"[{self.code}] {base}"
        return base


class WalletSecurityError(WalletError):
    """Security-sensitive failure (bad password, pickle blocked, etc.)."""


class WalletValidationError(WalletError):
    """Input validation failed (bad address, empty message, bad JSON, etc.)."""


class InvalidAddressError(WalletValidationError):
    """String is not a valid 20-byte hex Ethereum address."""


class TokenMathError(WalletValidationError):
    """Token amount rules violated (floats, mismatched decimals, etc.)."""
