"""Tests for :mod:`pricing.gas_cost` covering V2-only and V3-fallback paths."""

from __future__ import annotations

import pytest

from core.types import Address, Token
from pricing.gas_cost import gas_cost_in_output_token, gas_cost_wei
from pricing.liquidity_pool import QuoteResult
from pricing.uniswap_v2_pair import UniswapV2Pair

WETH = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
USDC = Token(Address("0x1111111111111111111111111111111111111111"), "USDC", 6)


def test_gas_cost_wei_basic() -> None:
    # 21_000 gas at 50 gwei -> 21_000 * 50 * 1e9 wei
    assert gas_cost_wei(21_000, 50) == 21_000 * 50 * 10**9


_POOL_ADDR = Address("0x" + "a" * 40)


def test_v2_path_unchanged_when_v3_pools_unset() -> None:
    pool = UniswapV2Pair(
        _POOL_ADDR,
        WETH,
        USDC,
        reserve0=100 * 10**18,
        reserve1=200_000 * 10**6,
        fee_bps=30,
    )
    out = gas_cost_in_output_token([pool], USDC, gas_wei=10**18, eth_price_in_output=None)
    # 1 ETH * (200_000 USDC / 100 WETH) ≈ 2_000 USDC raw atoms.
    assert out == 10**18 * pool.reserve1 // pool.reserve0


def test_v3_fallback_used_when_no_v2_match() -> None:
    """When token_out has no V2 WETH pair, a registered V3 WETH pool is queried."""

    class _StubV3:
        token0 = WETH
        token1 = USDC
        fee = 500

        def quote_exact_input(self, token_in, amount_in):
            # 1 WETH -> 2_000 USDC at this fee tier.
            assert token_in == WETH
            assert amount_in == 10**18
            return QuoteResult(amount_out=2_000 * 10**6, gas_estimate=120_000)

    out = gas_cost_in_output_token(
        v2_pools=[],
        token_out=USDC,
        gas_wei=10**18,  # 1 ETH worth of gas
        eth_price_in_output=None,
        v3_pools=[_StubV3()],
    )
    assert out == 2_000 * 10**6


def test_v3_fallback_only_when_v2_misses() -> None:
    """A working V2 WETH pool must take precedence over V3 (avoid extra eth_call)."""

    class _StubV3:
        token0 = WETH
        token1 = USDC
        fee = 500

        def quote_exact_input(self, token_in, amount_in):
            raise AssertionError("V3 quoter should not be called when V2 matches")

    pool = UniswapV2Pair(
        _POOL_ADDR,
        WETH,
        USDC,
        reserve0=100 * 10**18,
        reserve1=200_000 * 10**6,
        fee_bps=30,
    )
    out = gas_cost_in_output_token([pool], USDC, 10**18, None, v3_pools=[_StubV3()])
    assert out == 10**18 * pool.reserve1 // pool.reserve0


def test_raises_when_no_path_available() -> None:
    with pytest.raises(ValueError, match="Cannot convert gas"):
        gas_cost_in_output_token([], USDC, gas_wei=1, eth_price_in_output=None)
