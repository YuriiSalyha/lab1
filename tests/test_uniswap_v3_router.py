"""Unit tests for :mod:`chain.uniswap_v3_router` calldata encoders."""

from __future__ import annotations

import pytest
from eth_abi import decode as abi_decode

from chain.uniswap_v3_router import (
    DEFAULT_UNISWAP_V3_ROUTER,
    encode_exact_input_single_calldata,
    encode_exact_output_single_calldata,
    resolve_v3_swap_router,
)
from core.types import Address

TOKEN_IN = Address("0x1111111111111111111111111111111111111111")
TOKEN_OUT = Address("0x2222222222222222222222222222222222222222")
RECIPIENT = Address("0x3333333333333333333333333333333333333333")


def _decode_single_tuple(calldata: bytes) -> tuple:
    body = calldata[4:]
    (params,) = abi_decode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        body,
    )
    return params


def test_exact_input_single_selector_and_layout() -> None:
    cd = encode_exact_input_single_calldata(
        token_in=TOKEN_IN,
        token_out=TOKEN_OUT,
        fee=500,
        recipient=RECIPIENT,
        amount_in=10**18,
        amount_out_min=1,
    )
    assert cd[:4].hex() == "04e45aaf"
    params = _decode_single_tuple(cd)
    assert params[0].lower() == TOKEN_IN.lower
    assert params[1].lower() == TOKEN_OUT.lower
    assert params[2] == 500
    assert params[3].lower() == RECIPIENT.lower
    assert params[4] == 10**18
    assert params[5] == 1
    assert params[6] == 0


def test_exact_output_single_selector_and_layout() -> None:
    cd = encode_exact_output_single_calldata(
        token_in=TOKEN_IN,
        token_out=TOKEN_OUT,
        fee=3000,
        recipient=RECIPIENT,
        amount_out=10**18,
        amount_in_max=2 * 10**18,
    )
    assert cd[:4].hex() == "5023b4df"
    params = _decode_single_tuple(cd)
    assert params[2] == 3000
    assert params[4] == 10**18
    assert params[5] == 2 * 10**18


def test_encoders_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        encode_exact_input_single_calldata(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=500,
            recipient=RECIPIENT,
            amount_in=0,
            amount_out_min=0,
        )
    with pytest.raises(ValueError):
        encode_exact_output_single_calldata(
            token_in=TOKEN_IN,
            token_out=TOKEN_OUT,
            fee=0,
            recipient=RECIPIENT,
            amount_out=10**18,
            amount_in_max=2 * 10**18,
        )


def test_resolve_v3_swap_router_explicit_arg_wins(monkeypatch) -> None:
    monkeypatch.setenv("UNISWAP_V3_ROUTER", "0x9999999999999999999999999999999999999999")
    custom = Address("0x4444444444444444444444444444444444444444")
    assert resolve_v3_swap_router(custom) == custom


def test_resolve_v3_swap_router_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("UNISWAP_V3_ROUTER", "0x9999999999999999999999999999999999999999")
    out = resolve_v3_swap_router(None)
    assert out.lower == "0x9999999999999999999999999999999999999999"


def test_resolve_v3_swap_router_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("UNISWAP_V3_ROUTER", raising=False)
    assert resolve_v3_swap_router(None) == DEFAULT_UNISWAP_V3_ROUTER
