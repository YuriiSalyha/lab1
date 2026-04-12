"""Tests for :mod:`pricing.arbitrage_scanner`."""

from core.types import Address, Token
from pricing.arbitrage_scanner import ArbitrageScanner, default_amount_grid
from pricing.uniswap_v2_pair import UniswapV2Pair

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
A3 = Address("0x3333333333333333333333333333333333333333")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
P_B = Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
P_C = Address("0xcccccccccccccccccccccccccccccccccccccccc")


def _tok(addr: Address, sym: str) -> Token:
    return Token(address=addr, symbol=sym, decimals=18)


def test_default_amount_grid() -> None:
    g = default_amount_grid(max_raw=100, steps=10)
    assert 1 in g and 100 in g
    assert g == sorted(set(g))


def test_scanner_finds_no_arb_on_symmetric_triangle() -> None:
    """Equal reserves → no cyclic gain after fees."""
    t1, t2, t3 = _tok(A1, "A"), _tok(A2, "B"), _tok(A3, "C")
    p0 = UniswapV2Pair(PAIR, t1, t2, 10**24, 10**24, 30)
    p1 = UniswapV2Pair(P_B, t2, t3, 10**24, 10**24, 30)
    p2 = UniswapV2Pair(P_C, t3, t1, 10**24, 10**24, 30)
    scanner = ArbitrageScanner([p0, p1, p2], max_cycle_len=3, gas_price_gwei=0)
    opps = scanner.find_opportunities(amount_candidates=[10**18, 10**19])
    assert opps == []
