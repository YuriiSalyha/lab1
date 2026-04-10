"""Tests for :class:`pricing.route.Route` and :class:`pricing.route_finder.RouteFinder`."""

import pytest

from core.types import Address, Token
from pricing.route import Route
from pricing.route_finder import RouteFinder
from pricing.uniswap_v2_pair import UniswapV2Pair

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
A3 = Address("0x3333333333333333333333333333333333333333")
A4 = Address("0x4444444444444444444444444444444444444444")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def _tok(addr: Address, sym: str, dec: int = 18) -> Token:
    return Token(address=addr, symbol=sym, decimals=dec)


def test_route_output_matches_sequential_swaps() -> None:
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    p = UniswapV2Pair(
        address=PAIR,
        token0=weth,
        token1=usdc,
        reserve0=100 * 10**18,
        reserve1=200_000 * 10**6,
        fee_bps=30,
    )
    route = Route([p], [weth, usdc])
    amount = 10**18
    assert route.get_output(amount) == p.get_amount_out(amount, weth)
    inter = route.get_intermediate_amounts(amount)
    assert inter == [amount, route.get_output(amount)]
    assert route.estimate_gas() == 150_000


def test_direct_vs_multihop() -> None:
    """Multi-hop path yields higher gross output when direct pool is shallow."""
    shib = _tok(A1, "SHIB")
    weth = _tok(A2, "WETH")
    usdc = _tok(A3, "USDC", 6)

    # Direct SHIB/USDC: very little USDC depth → poor output
    direct = UniswapV2Pair(
        address=Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        token0=shib,
        token1=usdc,
        reserve0=10**30,
        reserve1=500 * 10**6,
        fee_bps=30,
    )
    shib_weth = UniswapV2Pair(
        address=Address("0xcccccccccccccccccccccccccccccccccccccccc"),
        token0=shib,
        token1=weth,
        reserve0=10**30,
        reserve1=5000 * 10**18,
        fee_bps=30,
    )
    weth_usdc = UniswapV2Pair(
        address=Address("0xdddddddddddddddddddddddddddddddddddddddd"),
        token0=weth,
        token1=usdc,
        reserve0=2000 * 10**18,
        reserve1=4_000_000 * 10**6,
        fee_bps=30,
    )

    finder = RouteFinder([direct, shib_weth, weth_usdc])
    amount_in = 10**24
    gas_low = 1

    best_route, net = finder.find_best_route(shib, usdc, amount_in, gas_low, max_hops=3)
    assert best_route is not None
    assert best_route.num_hops == 2
    assert weth in best_route.path

    gross_direct = direct.get_amount_out(amount_in, shib)
    gross_multi = best_route.get_output(amount_in)
    assert gross_multi > gross_direct


def test_gas_makes_direct_better() -> None:
    """Same topology: very high gas price makes the 1-hop route win on net output."""
    shib = _tok(A1, "SHIB")
    weth = _tok(A2, "WETH")
    usdc = _tok(A3, "USDC", 6)

    direct = UniswapV2Pair(
        address=Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        token0=shib,
        token1=usdc,
        reserve0=10**30,
        reserve1=500 * 10**6,
        fee_bps=30,
    )
    shib_weth = UniswapV2Pair(
        address=Address("0xcccccccccccccccccccccccccccccccccccccccc"),
        token0=shib,
        token1=weth,
        reserve0=10**30,
        reserve1=5000 * 10**18,
        fee_bps=30,
    )
    weth_usdc = UniswapV2Pair(
        address=Address("0xdddddddddddddddddddddddddddddddddddddddd"),
        token0=weth,
        token1=usdc,
        reserve0=2000 * 10**18,
        reserve1=4_000_000 * 10**6,
        fee_bps=30,
    )

    finder = RouteFinder([direct, shib_weth, weth_usdc])
    amount_in = 10**24

    _, net_cheap = finder.find_best_route(shib, usdc, amount_in, gas_price_gwei=1, max_hops=3)
    best_expensive, net_expensive = finder.find_best_route(
        shib, usdc, amount_in, gas_price_gwei=8_000, max_hops=3
    )

    assert net_cheap > net_expensive
    assert best_expensive is not None
    assert best_expensive.num_hops == 1


def test_no_route_exists() -> None:
    a = _tok(A1, "A")
    b = _tok(A2, "B")
    c = _tok(A3, "C")
    pool = UniswapV2Pair(
        address=PAIR,
        token0=a,
        token1=b,
        reserve0=10**18,
        reserve1=10**18,
        fee_bps=30,
    )
    finder = RouteFinder([pool])
    route, net = finder.find_best_route(a, c, 10**18, gas_price_gwei=10)
    assert route is None
    assert net == 0
    assert finder.find_all_routes(a, c) == []


def test_find_all_routes_respects_max_hops() -> None:
    """Diamond A—B—D and A—C—D: two 2-hop paths; reaching D in one pool is impossible."""
    a = _tok(A1, "A")
    b = _tok(A2, "B")
    c = _tok(A3, "C")
    d = _tok(A4, "D")
    p_ab = UniswapV2Pair(
        Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        a,
        b,
        10**24,
        10**24,
        30,
    )
    p_bd = UniswapV2Pair(
        Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        b,
        d,
        10**24,
        10**24,
        30,
    )
    p_ac = UniswapV2Pair(
        Address("0xcccccccccccccccccccccccccccccccccccccccc"),
        a,
        c,
        10**24,
        10**24,
        30,
    )
    p_cd = UniswapV2Pair(
        Address("0xdddddddddddddddddddddddddddddddddddddddd"),
        c,
        d,
        10**24,
        10**24,
        30,
    )
    finder = RouteFinder([p_ab, p_bd, p_ac, p_cd])
    routes2 = finder.find_all_routes(a, d, max_hops=2)
    assert len(routes2) == 2
    assert all(r.num_hops == 2 for r in routes2)
    routes1 = finder.find_all_routes(a, d, max_hops=1)
    assert routes1 == []


def test_compare_routes_sorted_by_net() -> None:
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    p = UniswapV2Pair(PAIR, weth, usdc, 10**22, 10**12, 30)
    finder = RouteFinder([p])
    rows = finder.compare_routes(weth, usdc, 10**18, gas_price_gwei=10)
    assert len(rows) == 1
    assert rows[0]["net_output"] == rows[0]["gross_output"] - rows[0]["gas_cost"]
    assert rows[0]["gas_cost_wei"] == rows[0]["gas_estimate"] * 10 * 10**9


def test_route_validation_rejects_mismatched_path() -> None:
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    dai = _tok(A4, "DAI")
    p = UniswapV2Pair(PAIR, weth, usdc, 10**20, 10**12, 30)
    with pytest.raises(ValueError, match="does not connect"):
        Route([p], [weth, dai])
