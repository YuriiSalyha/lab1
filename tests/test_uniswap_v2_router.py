"""Tests for :mod:`chain.uniswap_v2_router` (encoding + decoder selector parity)."""

from __future__ import annotations

import pytest
from eth_abi import encode

from chain.decoder import TransactionDecoder
from chain.uniswap_v2_router import (
    UNISWAP_V2_ROUTER_SWAP_ENTRIES,
    decode_swap_amounts_return_data,
    encode_uniswap_v2_swap_calldata,
)
from core.types import Address

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
A3 = Address("0x3333333333333333333333333333333333333333")


def test_swap_entries_match_decoder_selectors() -> None:
    for sel, meta in UNISWAP_V2_ROUTER_SWAP_ENTRIES.items():
        calldata = bytes.fromhex(sel) + b"\x00" * 32
        out = TransactionDecoder.decode_function_call(calldata)
        assert out["function"] == meta["name"]
        assert out["selector"] == sel


def test_encode_swap_exact_tokens_for_tokens_round_trip_decode() -> None:
    body = encode(
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [10**18, 0, [A1.checksum, A2.checksum], A3.checksum, 1_700_000_000],
    )
    expected = bytes.fromhex("38ed1739") + body
    got = encode_uniswap_v2_swap_calldata(
        "swapExactTokensForTokens",
        path=[A1, A2],
        to=A3,
        deadline=1_700_000_000,
        amount_in=10**18,
        amount_out_min=0,
    )
    assert got == expected
    dec = TransactionDecoder.decode_function_call(got)
    assert dec["function"] == "swapExactTokensForTokens"
    assert dec["params"]["amountIn"] == 10**18
    assert dec["params"]["amountOutMin"] == 0
    assert dec["params"]["deadline"] == 1_700_000_000


def test_encode_swap_exact_eth_for_tokens() -> None:
    got = encode_uniswap_v2_swap_calldata(
        "swapExactETHForTokens",
        path=[A1, A2],
        to=A3,
        deadline=123,
        amount_out_min=1,
    )
    assert got[:4].hex() == "7ff36ab5"
    dec = TransactionDecoder.decode_function_call(got)
    assert dec["function"] == "swapExactETHForTokens"
    assert dec["params"]["amountOutMin"] == 1


def test_encode_requires_params() -> None:
    with pytest.raises(ValueError, match="amount_in"):
        encode_uniswap_v2_swap_calldata(
            "swapExactTokensForTokens",
            path=[A1, A2],
            to=A3,
            deadline=1,
            amount_out_min=0,
        )


def test_decode_swap_amounts_return_data() -> None:
    raw = encode(["uint256[]"], [[100, 200, 300]])
    assert decode_swap_amounts_return_data(raw) == [100, 200, 300]


def test_decode_swap_amounts_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        decode_swap_amounts_return_data(b"")
