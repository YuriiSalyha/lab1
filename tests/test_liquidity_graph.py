"""Tests for :mod:`pricing.liquidity_graph`."""

from core.types import Address, Token
from pricing.liquidity_graph import build_adjacency, find_all_paths, find_simple_cycles
from pricing.liquidity_pool import as_liquidity_quote
from pricing.uniswap_v2_pair import UniswapV2Pair

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
A3 = Address("0x3333333333333333333333333333333333333333")
A4 = Address("0x4444444444444444444444444444444444444444")
PA = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
PB = Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
PC = Address("0xcccccccccccccccccccccccccccccccccccccccc")
PD = Address("0xdddddddddddddddddddddddddddddddddddddddd")


def _tok(addr: Address, sym: str) -> Token:
    return Token(address=addr, symbol=sym, decimals=18)


def test_triangle_has_one_cycle_up_to_three_pools() -> None:
    t1, t2, t3 = _tok(A1, "A"), _tok(A2, "B"), _tok(A3, "C")
    p0 = UniswapV2Pair(PA, t1, t2, 10**24, 10**24, 30)
    p1 = UniswapV2Pair(PB, t2, t3, 10**24, 10**24, 30)
    p2 = UniswapV2Pair(PC, t3, t1, 10**24, 10**24, 30)
    pools = [as_liquidity_quote(p) for p in (p0, p1, p2)]
    g = build_adjacency(pools)
    cycles = find_simple_cycles(g, max_cycle_len=3)
    assert len(cycles) == 1
    pl, pt = cycles[0]
    assert len(pl) == 3
    assert pt[0] == pt[-1] == t1
    assert [x.symbol for x in pt] == ["A", "B", "C", "A"]


def test_find_all_paths_matches_diamond() -> None:
    """Same topology as ``test_routes`` max_hops diamond."""
    a, b, c, d = _tok(A1, "A"), _tok(A2, "B"), _tok(A3, "C"), _tok(A4, "D")
    p_ab = UniswapV2Pair(PA, a, b, 10**24, 10**24, 30)
    p_bd = UniswapV2Pair(PB, b, d, 10**24, 10**24, 30)
    p_ac = UniswapV2Pair(PC, a, c, 10**24, 10**24, 30)
    p_cd = UniswapV2Pair(PD, c, d, 10**24, 10**24, 30)
    pools = [as_liquidity_quote(p) for p in (p_ab, p_bd, p_ac, p_cd)]
    g = build_adjacency(pools)
    paths = find_all_paths(g, a, d, max_hops=2)
    assert len(paths) == 2
