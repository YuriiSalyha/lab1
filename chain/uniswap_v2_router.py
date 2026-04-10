"""Uniswap V2 router swap calldata encoding and return-data decoding.

Selector bytes and ABI layouts match :mod:`chain.decoder` / ``_FUNCTION_SELECTORS``.
"""

from __future__ import annotations

from typing import Any, Final, Sequence

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from core.types import Address

# Merged into :data:`chain.decoder._FUNCTION_SELECTORS` — keep in sync.
UNISWAP_V2_ROUTER_SWAP_ENTRIES: dict[str, dict[str, Any]] = {
    "38ed1739": {
        "name": "swapExactTokensForTokens",
        "types": ["uint256", "uint256", "address[]", "address", "uint256"],
        "param_names": ["amountIn", "amountOutMin", "path", "to", "deadline"],
    },
    "7ff36ab5": {
        "name": "swapExactETHForTokens",
        "types": ["uint256", "address[]", "address", "uint256"],
        "param_names": ["amountOutMin", "path", "to", "deadline"],
    },
    "18cbafe5": {
        "name": "swapExactTokensForETH",
        "types": ["uint256", "uint256", "address[]", "address", "uint256"],
        "param_names": ["amountIn", "amountOutMin", "path", "to", "deadline"],
    },
    "fb3bdb41": {
        "name": "swapETHForExactTokens",
        "types": ["uint256", "address[]", "address", "uint256"],
        "param_names": ["amountOut", "path", "to", "deadline"],
    },
    "8803dbee": {
        "name": "swapTokensForExactTokens",
        "types": ["uint256", "uint256", "address[]", "address", "uint256"],
        "param_names": ["amountOut", "amountInMax", "path", "to", "deadline"],
    },
    "4a25d94a": {
        "name": "swapTokensForExactETH",
        "types": ["uint256", "uint256", "address[]", "address", "uint256"],
        "param_names": ["amountOut", "amountInMax", "path", "to", "deadline"],
    },
}

UNISWAP_V2_SWAP_FUNCTION_NAMES: Final[frozenset[str]] = frozenset(
    {e["name"] for e in UNISWAP_V2_ROUTER_SWAP_ENTRIES.values()}
)

_NAME_TO_SELECTOR: dict[str, str] = {
    meta["name"]: sel for sel, meta in UNISWAP_V2_ROUTER_SWAP_ENTRIES.items()
}


def _checksum_addr(addr: Address | str) -> str:
    if isinstance(addr, Address):
        return addr.checksum
    return to_checksum_address(addr)


def _norm_path(path: Sequence[Address | str]) -> list[str]:
    return [_checksum_addr(p) for p in path]


def encode_uniswap_v2_swap_calldata(
    function: str,
    *,
    path: Sequence[Address | str],
    to: Address | str,
    deadline: int,
    amount_in: int | None = None,
    amount_out_min: int | None = None,
    amount_out: int | None = None,
    amount_in_max: int | None = None,
) -> bytes:
    """ABI-encode a Uniswap V2 router swap call (selector + body).

    Args:
        function: Router method name (e.g. ``swapExactTokensForTokens``).
        path: Token addresses along the route.
        to: Recipient of output tokens.
        deadline: Unix timestamp (uint256); use a large value for simulations.
        amount_in: For exact-in swaps.
        amount_out_min: Minimum out for exact-in swaps.
        amount_out: Exact out for exact-output swaps.
        amount_in_max: Maximum in for exact-output swaps.
    """
    if function not in _NAME_TO_SELECTOR:
        raise ValueError(f"unsupported Uniswap V2 swap function: {function!r}")
    selector_hex = _NAME_TO_SELECTOR[function]
    spec = UNISWAP_V2_ROUTER_SWAP_ENTRIES[selector_hex]
    types: list[str] = list(spec["types"])
    p = _norm_path(path)
    t = _checksum_addr(to)
    d = int(deadline)

    if function == "swapExactTokensForTokens":
        if amount_in is None or amount_out_min is None:
            raise ValueError("swapExactTokensForTokens requires amount_in and amount_out_min")
        values = (int(amount_in), int(amount_out_min), p, t, d)
    elif function == "swapExactETHForTokens":
        if amount_out_min is None:
            raise ValueError("swapExactETHForTokens requires amount_out_min")
        values = (int(amount_out_min), p, t, d)
    elif function == "swapExactTokensForETH":
        if amount_in is None or amount_out_min is None:
            raise ValueError("swapExactTokensForETH requires amount_in and amount_out_min")
        values = (int(amount_in), int(amount_out_min), p, t, d)
    elif function == "swapETHForExactTokens":
        if amount_out is None:
            raise ValueError("swapETHForExactTokens requires amount_out")
        values = (int(amount_out), p, t, d)
    elif function == "swapTokensForExactTokens":
        if amount_out is None or amount_in_max is None:
            raise ValueError("swapTokensForExactTokens requires amount_out and amount_in_max")
        values = (int(amount_out), int(amount_in_max), p, t, d)
    elif function == "swapTokensForExactETH":
        if amount_out is None or amount_in_max is None:
            raise ValueError("swapTokensForExactETH requires amount_out and amount_in_max")
        values = (int(amount_out), int(amount_in_max), p, t, d)
    else:
        raise ValueError(f"unsupported Uniswap V2 swap function: {function!r}")

    return bytes.fromhex(selector_hex) + abi_encode(types, values)


def decode_swap_amounts_return_data(return_data: bytes) -> list[int]:
    """Decode ``uint256[]`` return from Uniswap V2 swap view/static/simulation."""
    if not return_data:
        raise ValueError("empty return data")
    amounts: tuple[Any, ...] = abi_decode(["uint256[]"], return_data)
    return [int(x) for x in amounts[0]]
