"""Small shared helpers for the ``chain`` package (formatting, token metadata).

Kept dependency-light so modules can import without circular Web3 coupling.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

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
    if raw is None:
        return f"? {symbol}"
    human = Decimal(raw) / Decimal(10**decimals)
    return f"{human:,.4f} {symbol}"
