"""Small shared helpers for the ``chain`` package (formatting, token metadata).

Kept dependency-light so modules can import without circular Web3 coupling.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

from chain.errors import InvalidParameterError
from chain.validation import validate_token_address_str

logger = logging.getLogger(__name__)


class _HasTokenCache(Protocol):
    """Minimal interface for objects that expose ``token_cache.get``."""

    token_cache: Any


def token_symbol_and_decimals(client: _HasTokenCache, token_address: str) -> tuple[str, int]:
    """Resolve ERC-20 ``symbol`` and ``decimals`` via the client's cache/RPC.

    Args:
        client: Object with ``token_cache`` (e.g. ``ChainClient``).
        token_address: Token contract address (checksummed or not).

    Returns:
        ``(symbol, decimals)`` with ``"???"`` / ``18`` on failure.
    """
    validate_token_address_str(token_address)
    try:
        meta = client.token_cache.get(token_address)
        sym = str(meta.get("symbol", "???"))
        dec = int(meta.get("decimals", 18))
        logger.debug(
            "token meta: addr_suffix=%s symbol=%s decimals=%s",
            token_address[-8:],
            sym,
            dec,
        )
        return sym, dec
    except Exception as err:
        logger.warning("token metadata fetch failed: %s", err)
        return "???", 18


def format_human_token_amount(raw: int | None, decimals: int, symbol: str) -> str:
    """Format a raw uint256 balance as ``\"1,234.5678 SYM\"``."""
    if not isinstance(decimals, int) or isinstance(decimals, bool):
        raise InvalidParameterError("decimals must be an integer.")
    if decimals < 0:
        raise InvalidParameterError(f"decimals must be non-negative, got {decimals}.")
    if not isinstance(symbol, str):
        raise InvalidParameterError("symbol must be a string.")
    if raw is not None and (not isinstance(raw, int) or isinstance(raw, bool)):
        raise InvalidParameterError("raw amount must be an integer or None.")
    if raw is None:
        return f"? {symbol}"
    human = Decimal(raw) / Decimal(10**decimals)
    return f"{human:,.4f} {symbol}"
