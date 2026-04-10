"""Unit tests for :mod:`pricing.pricing_engine` (mocked RPC / simulator)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.fork_simulator import SimulationResult
from pricing.parsed_swap import ParsedSwap
from pricing.pricing_engine import DEFAULT_UNISWAP_V2_ROUTER, PricingEngine, Quote, QuoteError
from pricing.route_finder import RouteFinder
from pricing.uniswap_v2_pair import UniswapV2Pair

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
A3 = Address("0x3333333333333333333333333333333333333333")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
PAIR2 = Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
SENDER = Address("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")


def _tok(a: Address, sym: str, dec: int = 18) -> Token:
    return Token(address=a, symbol=sym, decimals=dec)


def _make_engine_with_pools(
    pools: list[UniswapV2Pair],
) -> PricingEngine:
    client = MagicMock()
    eng = PricingEngine(client, "http://127.0.0.1:9", "ws://127.0.0.1:0", SENDER)
    eng.pools = {p.address: p for p in pools}
    eng.route_finder = RouteFinder(pools)
    return eng


def _pair(
    addr: Address = PAIR,
    r0: int = 100 * 10**18,
    r1: int = 200_000 * 10**6,
) -> UniswapV2Pair:
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    return UniswapV2Pair(
        address=addr,
        token0=weth,
        token1=usdc,
        reserve0=r0,
        reserve1=r1,
        fee_bps=30,
    )


def test_get_quote_raises_when_no_pools() -> None:
    eng = PricingEngine(MagicMock(), "http://x", "ws://x", SENDER)
    t0 = _tok(A1, "A")
    t1 = _tok(A2, "B")
    with pytest.raises(QuoteError, match="No pools loaded"):
        eng.get_quote(t0, t1, 10**18, gas_price_gwei=50)


def test_get_quote_raises_when_no_route() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    orphan = _tok(A3, "ORPH")
    with pytest.raises(QuoteError, match="No route"):
        eng.get_quote(orphan, p.token1, 10**18, gas_price_gwei=50)


def test_get_quote_raises_on_simulation_failure() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    amount = 10**18

    with patch.object(
        eng.simulator,
        "simulate_route",
        return_value=SimulationResult(
            success=False,
            amount_out=0,
            gas_used=0,
            error="revert",
            logs=[],
        ),
    ):
        with pytest.raises(QuoteError, match="Simulation failed"):
            eng.get_quote(p.token0, p.token1, amount, gas_price_gwei=1)


def test_get_quote_happy_path_and_is_valid() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    amount = 10**18
    gross = p.get_amount_out(amount, p.token0)

    with patch.object(
        eng.simulator,
        "simulate_route",
        return_value=SimulationResult(
            success=True,
            amount_out=gross,
            gas_used=160_000,
            error=None,
            logs=[],
        ),
    ):
        q = eng.get_quote(p.token0, p.token1, amount, gas_price_gwei=1)

    assert isinstance(q, Quote)
    assert q.gross_output == gross
    assert q.simulated_output == gross
    assert q.gas_estimate == 160_000
    assert q.is_valid() is True
    assert eng.swap_router == DEFAULT_UNISWAP_V2_ROUTER


def test_quote_is_valid_false_when_simulation_diverges() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    amount = 10**18
    gross = p.get_amount_out(amount, p.token0)

    with patch.object(
        eng.simulator,
        "simulate_route",
        return_value=SimulationResult(
            success=True,
            amount_out=gross // 2,
            gas_used=1,
            error=None,
            logs=[],
        ),
    ):
        q = eng.get_quote(p.token0, p.token1, amount, gas_price_gwei=1)

    assert q.is_valid() is False


def test_quote_is_valid_false_for_zero_gross() -> None:
    q = Quote(
        route=MagicMock(),
        amount_in=0,
        gross_output=0,
        net_output=0,
        simulated_output=0,
        gas_estimate=0,
        timestamp=0.0,
    )
    assert q.is_valid() is False


def test_refresh_pool_updates_pair_and_rebuilds_finder() -> None:
    p1 = _pair(PAIR, r0=100 * 10**18, r1=200_000 * 10**6)
    p2 = _pair(PAIR2, r0=100 * 10**18, r1=200_000 * 10**6)
    client = MagicMock()
    eng = PricingEngine(client, "http://x", "ws://x", SENDER)
    eng.pools = {PAIR: p1, PAIR2: p2}
    eng.route_finder = RouteFinder([p1, p2])
    old_finder = eng.route_finder

    p1_new = _pair(PAIR, r0=1000 * 10**18, r1=2_000_000 * 10**6)

    with patch.object(UniswapV2Pair, "from_chain", return_value=p1_new):
        eng.refresh_pool(PAIR)

    assert eng.pools[PAIR] is p1_new
    assert eng.route_finder is not old_finder
    assert eng.route_finder.pools is not old_finder.pools


def test_refresh_pool_unknown_raises() -> None:
    eng = _make_engine_with_pools([_pair()])
    with pytest.raises(QuoteError, match="Pool not loaded"):
        eng.refresh_pool(PAIR2)


def test_affected_pool_addresses_and_mempool_callback() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    swap_ok = ParsedSwap(
        tx_hash="0x01",
        router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        dex="UniswapV2",
        method="swapExactTokensForTokens",
        token_in=p.token0.address,
        token_out=p.token1.address,
        amount_in=1,
        min_amount_out=0,
        deadline=1,
        sender=SENDER,
        gas_price=0,
    )
    assert eng.affected_pool_addresses(swap_ok) == [PAIR]

    swap_miss = ParsedSwap(
        tx_hash="0x02",
        router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        dex="UniswapV2",
        method="swapExactTokensForTokens",
        token_in=A3,
        token_out=Address("0x4444444444444444444444444444444444444444"),
        amount_in=1,
        min_amount_out=0,
        deadline=1,
        sender=SENDER,
        gas_price=0,
    )
    assert eng.affected_pool_addresses(swap_miss) == []

    eng._on_mempool_swap(swap_ok)
    assert len(eng._mempool_affects) == 1
    eng._on_mempool_swap(swap_miss)
    assert len(eng._mempool_affects) == 1


def test_load_pools_calls_from_chain() -> None:
    client = MagicMock()
    eng = PricingEngine(client, "http://x", "ws://x", SENDER)
    fake = _pair()

    with patch.object(UniswapV2Pair, "from_chain", return_value=fake) as fc:
        eng.load_pools([PAIR])

    fc.assert_called_once_with(PAIR, client)
    assert eng.pools[PAIR] is fake
    assert eng.route_finder is not None
