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


def test_symbol_match_usdt_usdc_interchangeable():
    usdc = Token(Address("0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"), "USDC", 6)
    assert symbol_match("USDT", usdc)
    assert symbol_match("USDC", usdc)
    usdt = Token(Address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"), "USDT", 6)
    assert symbol_match("USDC", usdt)


def test_find_pool_sushi_usdt_matches_sushi_usdc_pool():
    sushi = Token(Address("0xd4d42F0b6DEF4CE0383636770eF773390d85c61A"), "SUSHI", 18)
    usdc = Token(Address("0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"), "USDC", 6)
    pool = UniswapV2Pair(
        PAIR,
        sushi,
        usdc,
        reserve0=10**24,
        reserve1=10**12,
        fee_bps=30,
    )
    pools = {PAIR: pool}
    assert find_pool_for_pair(pools, "SUSHI", "USDT") is pool
    base_t, quote_t = base_quote_tokens(pool, "SUSHI", "USDT")
    assert base_t.symbol == "SUSHI"
    assert quote_t.symbol == "USDC"


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


def test_symbol_match_arbitrum_usdt0_glyph():
    """Arbitrum bridged Tether registers as ``USD₮0`` (Tether glyph) — must match plain USDT."""
    weird = Token(Address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"), "USD\u20ae0", 6)
    assert symbol_match("USDT", weird)
    assert symbol_match("USDC", weird)
    ascii_variant = Token(Address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"), "USDT0", 6)
    assert symbol_match("USDT", ascii_variant)


class _StubV3:
    """Tiny V3 quoter stand-in for resolver tests (only token0/token1/fee/address read)."""

    def __init__(self, addr: Address, t0: Token, t1: Token, fee: int) -> None:
        self.address = addr
        self.token0 = t0
        self.token1 = t1
        self.fee = fee


def test_find_v3_pools_for_pair_returns_all_matching_tiers():
    from strategy.dex_token_resolver import find_v3_pools_for_pair

    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    usdc = Token(Address("0x1111111111111111111111111111111111111111"), "USDC", 6)
    a500 = _StubV3(Address("0xa1" + "0" * 38), weth, usdc, 500)
    a3000 = _StubV3(Address("0xa2" + "0" * 38), usdc, weth, 3000)
    a10000 = _StubV3(Address("0xa3" + "0" * 38), weth, usdc, 10000)
    pools = {p.address: p for p in (a500, a3000, a10000)}
    matches = find_v3_pools_for_pair(pools, "ETH", "USDT")
    assert len(matches) == 3


def test_find_candidates_for_pair_unions_v2_and_v3():
    from strategy.dex_token_resolver import find_candidates_for_pair

    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    usdt = Token(Address("0x1111111111111111111111111111111111111111"), "USDT", 6)
    v2 = UniswapV2Pair(
        PAIR,
        weth,
        usdt,
        reserve0=1000 * 10**18,
        reserve1=2_000_000 * 10**6,
        fee_bps=30,
    )
    v3 = _StubV3(Address("0xa1" + "0" * 38), weth, usdt, 500)
    cands = find_candidates_for_pair({PAIR: v2}, {v3.address: v3}, "ETH", "USDT")
    assert v2 in cands
    assert v3 in cands


def test_token_resolver_falls_back_to_v3_when_no_v2():
    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    usdt = Token(Address("0x1111111111111111111111111111111111111111"), "USDT", 6)
    v3 = _StubV3(Address("0xa1" + "0" * 38), weth, usdt, 500)
    engine = type("PE", (), {"pools": {}, "v3_pools": {v3.address: v3}})()
    resolve = token_resolver_from_pricing_engine(engine)  # type: ignore[arg-type]
    b, q = resolve("eth/usdt")
    assert b.symbol == "WETH"
    assert q.symbol == "USDT"


def test_token_resolver_prefers_v2_when_both_exist():
    """V2-only deployments must keep their behaviour; V2 wins when both pools match."""
    pool = _eth_usdt_pool()
    v3 = _StubV3(Address("0xa1" + "0" * 38), pool.token0, pool.token1, 500)
    engine = type("PE", (), {"pools": {PAIR: pool}, "v3_pools": {v3.address: v3}})()
    resolve = token_resolver_from_pricing_engine(engine)  # type: ignore[arg-type]
    b, q = resolve("eth/usdt")
    # V2 pool's WETH symbol is what comes back when V2 takes precedence.
    assert b.symbol == "WETH"
    assert q.symbol == "USDT"


def test_find_pool_eth_usdt_matches_arb_usdt_glyph_pool():
    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    arb_usdt = Token(Address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"), "USD\u20ae0", 6)
    pool = UniswapV2Pair(
        PAIR,
        weth,
        arb_usdt,
        reserve0=1000 * 10**18,
        reserve1=2_000_000 * 10**6,
        fee_bps=30,
    )
    pools = {PAIR: pool}
    assert find_pool_for_pair(pools, "ETH", "USDT") is pool
    base_t, quote_t = base_quote_tokens(pool, "ETH", "USDT")
    assert base_t.symbol == "WETH"
    assert quote_t.symbol == "USD\u20ae0"
