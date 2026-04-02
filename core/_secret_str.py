"""Holds raw secret strings without exposing them in ``repr``/``str``/pickles."""

from __future__ import annotations

from core.errors import WalletSecurityError

_MASKED = "***REDACTED***"


class SecretStr:
    """Wrap a secret string so logs and ``repr`` never show the raw value.

    Use :meth:`get_secret_value` when the real value is required for crypto ops.
    Equality compares underlying bytes; hashing is supported for use as dict keys.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """
        Args:
            value: Raw secret (e.g. hex private key).
        """
        self._value = value

    def get_secret_value(self) -> str:
        """Return the raw secret (use only where necessary)."""
        return self._value

    def __str__(self) -> str:
        return _MASKED

    def __repr__(self) -> str:
        return _MASKED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self) -> None:
        raise WalletSecurityError("Cannot pickle secret values")
