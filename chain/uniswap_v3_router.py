"""Uniswap V3 SwapRouter02 calldata encoding.

Encodes ``exactInputSingle`` and ``exactOutputSingle`` for the canonical
SwapRouter02 deployments. Selectors and ABI tuple layouts must stay in sync
with the on-chain contract — see
https://docs.uniswap.org/contracts/v3/reference/periphery/SwapRouter.

Mirrors :mod:`chain.uniswap_v2_router` so :mod:`executor.live_dex_leg` can
dispatch V2 vs V3 swap calldata behind one builder.
"""

from __future__ import annotations

from typing import Final

from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from core.types import Address

# Mainnet Uniswap V3 SwapRouter02 (also deployed at the same address on Arbitrum One).
DEFAULT_UNISWAP_V3_ROUTER = Address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")

# selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
_EXACT_INPUT_SINGLE_SELECTOR: Final[bytes] = bytes.fromhex("04e45aaf")
# selector("exactOutputSingle((address,address,uint24,address,uint256,uint256,uint160))")
_EXACT_OUTPUT_SINGLE_SELECTOR: Final[bytes] = bytes.fromhex("5023b4df")

# SwapRouter02 dropped the trailing ``deadline`` field that SwapRouter (v1) carried;
# the off-chain ``Multicall`` wrapper enforces a deadline at the top level instead.
_EXACT_INPUT_SINGLE_TYPES: Final[list[str]] = [
    "(address,address,uint24,address,uint256,uint256,uint160)",
]
_EXACT_OUTPUT_SINGLE_TYPES: Final[list[str]] = [
    "(address,address,uint24,address,uint256,uint256,uint160)",
]


def _checksum_addr(addr: Address | str) -> str:
    if isinstance(addr, Address):
        return addr.checksum
    return to_checksum_address(addr)


def encode_exact_input_single_calldata(
    *,
    token_in: Address | str,
    token_out: Address | str,
    fee: int,
    recipient: Address | str,
    amount_in: int,
    amount_out_min: int,
    sqrt_price_limit_x96: int = 0,
) -> bytes:
    """ABI-encode ``SwapRouter02.exactInputSingle`` (selector + tuple body)."""
    if amount_in <= 0:
        raise ValueError("amount_in must be positive")
    if amount_out_min < 0:
        raise ValueError("amount_out_min must be >= 0")
    if fee <= 0:
        raise ValueError("fee tier must be positive")
    params = (
        _checksum_addr(token_in),
        _checksum_addr(token_out),
        int(fee),
        _checksum_addr(recipient),
        int(amount_in),
        int(amount_out_min),
        int(sqrt_price_limit_x96),
    )
    body = abi_encode(_EXACT_INPUT_SINGLE_TYPES, [params])
    return _EXACT_INPUT_SINGLE_SELECTOR + body


def encode_exact_output_single_calldata(
    *,
    token_in: Address | str,
    token_out: Address | str,
    fee: int,
    recipient: Address | str,
    amount_out: int,
    amount_in_max: int,
    sqrt_price_limit_x96: int = 0,
) -> bytes:
    """ABI-encode ``SwapRouter02.exactOutputSingle`` (selector + tuple body)."""
    if amount_out <= 0:
        raise ValueError("amount_out must be positive")
    if amount_in_max <= 0:
        raise ValueError("amount_in_max must be positive")
    if fee <= 0:
        raise ValueError("fee tier must be positive")
    params = (
        _checksum_addr(token_in),
        _checksum_addr(token_out),
        int(fee),
        _checksum_addr(recipient),
        int(amount_out),
        int(amount_in_max),
        int(sqrt_price_limit_x96),
    )
    body = abi_encode(_EXACT_OUTPUT_SINGLE_TYPES, [params])
    return _EXACT_OUTPUT_SINGLE_SELECTOR + body


def resolve_v3_swap_router(swap_router: Address | None) -> Address:
    """Pick the SwapRouter02 address from arg → ``UNISWAP_V3_ROUTER`` env → mainnet default."""
    if swap_router is not None:
        return swap_router
    import os

    raw = os.getenv("UNISWAP_V3_ROUTER", "").strip()
    if raw:
        return Address.from_string(raw)
    return DEFAULT_UNISWAP_V3_ROUTER
