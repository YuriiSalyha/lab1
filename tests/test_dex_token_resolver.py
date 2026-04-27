"""Tests for :mod:`strategy.dex_token_resolver`."""

from __future__ import annotations

import pytest

from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair
from strategy.dex_token_resolver import (
    base_quote_tokens,
    find_pool_for_pair,
    symbol_match,
    token_resolver_from_pricing_engine,
)

PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def _eth_usdt_pool() -> UniswapV2Pair:
    usdt = Token(Address("0x1111111111111111111111111111111111111111"), "USDT", 6)
    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    return UniswapV2Pair(
        PAIR,
        usdt,
        weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
        fee_bps=30,
    )


def test_symbol_match_eth_weth():
    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    assert symbol_match("ETH", weth)
    assert symbol_match("WETH", weth)


def test_symbol_match_btc_wbtc():
    wbtc = Token(Address("0x3333333333333333333333333333333333333333"), "WBTC", 8)
    assert symbol_match("BTC", wbtc)
    assert symbol_match("WBTC", wbtc)


def test_find_pool_and_base_quote_order():
    pool = _eth_usdt_pool()
    pools = {PAIR: pool}
    assert find_pool_for_pair(pools, "ETH", "USDT") is pool
    base_t, quote_t = base_quote_tokens(pool, "ETH", "USDT")
    assert base_t.symbol == "WETH"
    assert quote_t.symbol == "USDT"


def test_token_resolver_from_engine():
    pool = _eth_usdt_pool()
    engine = type("PE", (), {"pools": {PAIR: pool}})()
    resolve = token_resolver_from_pricing_engine(engine)  # type: ignore[arg-type]
    b, q = resolve("eth/usdt")
    assert b.symbol == "WETH"
    assert q.symbol == "USDT"


def test_find_pool_missing_raises():
    with pytest.raises(ValueError, match="No Uniswap"):
        find_pool_for_pair({}, "ETH", "USDT")
