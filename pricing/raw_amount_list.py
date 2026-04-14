"""Parse lists of raw on-chain integer amounts from CLI strings (PowerShell-friendly)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_raw_amount_ints(s: str) -> list[int]:
    """
    Split on commas and/or ASCII whitespace so these are equivalent:

    - ``1e18,5e18``
    - ``1e18 5e18``
    - ``1e18, 5e18`` (comma lost or mangled by the shell → still works if tokens stay separated)

    Raises:
        ValueError: empty after strip, or a token is not a valid non-negative integer.
    """
    if not s.strip():
        raise ValueError("amount list must be non-empty")
    out: list[int] = []
    for part in s.replace(",", " ").split():
        p = part.strip().replace("_", "")
        if not p:
            continue
        try:
            v = int(Decimal(p))
        except (InvalidOperation, ValueError) as e:
            raise ValueError(f"Invalid raw amount {p!r} (use integer raw units)") from e
        if v <= 0:
            raise ValueError(f"Amount must be positive, got {p!r}")
        out.append(v)
    if not out:
        raise ValueError("amount list must contain at least one positive integer")
    return out
