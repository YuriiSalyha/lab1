from core.errors import WalletSecurityError

_MASKED = "***REDACTED***"


class _secret_str:
    """Wrapper that prevents accidental exposure of secret values in logs, repr, or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return _MASKED

    def __repr__(self) -> str:
        return _MASKED

    def __eq__(self, other):
        if isinstance(other, _secret_str):
            return self._value == other._value
        return NotImplemented

    def __hash__(self):
        return hash(self._value)

    def __reduce__(self):
        raise WalletSecurityError("Cannot pickle secret values")
