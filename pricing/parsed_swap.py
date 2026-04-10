"""Parse Uniswap V2 router swap calldata into :class:`ParsedSwap`."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Optional

from eth_utils import to_checksum_address

from core.types import Address

# Router methods decoded by :mod:`chain.decoder` for Uniswap V2–style swaps.
UNISWAP_V2_ROUTER_SWAP_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "swapExactTokensForTokens",
        "swapExactETHForTokens",
        "swapExactTokensForETH",
        "swapETHForExactTokens",
        "swapTokensForExactTokens",
        "swapTokensForExactETH",
    }
)


def _coerce_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    return int(v)


def _checksum_address_field(addr: Any) -> str:
    if isinstance(addr, str):
        return to_checksum_address(addr)
    if hasattr(addr, "hex"):
        hx = addr.hex()
        if not hx.startswith("0x"):
            hx = "0x" + hx
        return to_checksum_address(hx)
    return to_checksum_address(str(addr))


def _normalize_tx_hash(tx: dict[str, Any]) -> str:
    h = tx.get("hash")
    if h is None:
        return ""
    if hasattr(h, "hex"):
        hx = h.hex()
    else:
        hx = str(h)
    if hx and not hx.startswith("0x"):
        hx = "0x" + hx
    return hx


@dataclass
class ParsedSwap:
    """Parsed Uniswap V2–style router swap from mempool / pending tx."""

    tx_hash: str
    router: str
    dex: str
    method: str
    token_in: Optional[Address]
    token_out: Optional[Address]
    amount_in: int
    min_amount_out: int
    deadline: int
    sender: Address
    gas_price: int

    @property
    def slippage_tolerance(self) -> Optional[Decimal]:
        """Implied slippage as a fraction (e.g. ``Decimal('0.01')`` = 1%).

        Not computable from calldata alone (needs quoted ``amountOut`` / pool state).
        """
        return None


def try_parse_uniswap_v2_swap(
    tx: dict[str, Any],
    decoded: dict[str, Any],
) -> Optional[ParsedSwap]:
    """Build ``ParsedSwap`` for a known Uniswap V2 router swap, else ``None``."""

    func = decoded.get("function")
    if func not in UNISWAP_V2_ROUTER_SWAP_FUNCTIONS:
        return None

    params = decoded.get("params")
    if not isinstance(params, dict):
        return None

    path = params.get("path")
    if not path or len(path) < 2:
        return None

    to_raw = tx.get("to")
    if not to_raw:
        return None

    frm = tx.get("from")
    if not frm:
        return None

    deadline = _coerce_int(params.get("deadline"))

    amount_in: int
    min_amount_out: int

    if func == "swapExactTokensForTokens":
        amount_in = _coerce_int(params.get("amountIn"))
        min_amount_out = _coerce_int(params.get("amountOutMin"))
    elif func == "swapExactETHForTokens":
        amount_in = _coerce_int(tx.get("value"))
        min_amount_out = _coerce_int(params.get("amountOutMin"))
    elif func == "swapExactTokensForETH":
        amount_in = _coerce_int(params.get("amountIn"))
        min_amount_out = _coerce_int(params.get("amountOutMin"))
    elif func == "swapETHForExactTokens":
        # max ETH supplied; amountOut is exact desired output (stored in min_amount_out field).
        amount_in = _coerce_int(tx.get("value"))
        min_amount_out = _coerce_int(params.get("amountOut"))
    elif func == "swapTokensForExactTokens":
        amount_in = _coerce_int(params.get("amountInMax"))
        min_amount_out = _coerce_int(params.get("amountOut"))
    elif func == "swapTokensForExactETH":
        amount_in = _coerce_int(params.get("amountInMax"))
        min_amount_out = _coerce_int(params.get("amountOut"))
    else:
        return None

    gas_price = _coerce_int(tx.get("maxFeePerGas")) or _coerce_int(tx.get("gasPrice"))

    try:
        token_in = Address.from_string(path[0])
        token_out = Address.from_string(path[-1])
        sender = Address.from_string(_checksum_address_field(frm))
    except Exception:
        return None

    return ParsedSwap(
        tx_hash=_normalize_tx_hash(tx),
        router=_checksum_address_field(to_raw),
        dex="UniswapV2",
        method=func,
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        min_amount_out=min_amount_out,
        deadline=deadline,
        sender=sender,
        gas_price=gas_price,
    )
