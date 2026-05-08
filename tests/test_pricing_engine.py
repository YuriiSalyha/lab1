"""Unit tests for :mod:`pricing.pricing_engine` (mocked RPC / simulator)."""

from __future__ import annotations

from decimal import Decimal
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
        router=DEFAULT_UNISWAP_V2_ROUTER.checksum,
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
        router=DEFAULT_UNISWAP_V2_ROUTER.checksum,
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


# --- Math-only pair price quote ---------------------------------------------


def test_get_spot_quote_per_base_human_matches_reserve_ratio() -> None:
    """Spot quote is reserve ratio in human units (no fee, no trade size)."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p])
    spot = eng.get_spot_quote_per_base_human(p.token0, p.token1)
    assert spot == Decimal("2000")


def test_get_pair_prices_math_returns_real_pool_prices() -> None:
    """Math path must hit the constant-product formula, not a stub."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)  # WETH=100, USDC=200_000
    eng = _make_engine_with_pools([p])
    base = p.token0  # WETH
    quote = p.token1  # USDC
    size = Decimal("1")  # 1 WETH

    dex_buy, dex_sell, spot = eng.get_pair_prices_math(base, quote, size)

    # Spot price = 200_000 / 100 = 2000 USDC/WETH. With ~0.3% fee + curvature,
    # buying 1 WETH costs more than 2000 and selling yields less.
    assert spot == Decimal("2000")
    assert dex_sell < Decimal("2000") < dex_buy
    # Sanity: differ by at most a few %, both within an order of magnitude.
    assert Decimal("1900") < dex_sell < Decimal("2000")
    assert Decimal("2000") < dex_buy < Decimal("2100")


def test_get_pair_prices_math_is_size_dependent() -> None:
    """Bigger sizes should impact the price more (slippage)."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p])
    base = p.token0
    quote = p.token1

    small_buy, small_sell, _s0 = eng.get_pair_prices_math(base, quote, Decimal("0.01"))
    big_buy, big_sell, _s1 = eng.get_pair_prices_math(base, quote, Decimal("5"))

    # Buying more pushes the price you pay UP; selling more pushes the price
    # you receive DOWN. Both sides must move past the small-size quote.
    assert big_buy > small_buy
    assert big_sell < small_sell


def test_get_pair_prices_math_matches_pool_get_amount() -> None:
    """Returned prices must equal the integer math from the pair directly."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p])
    base, quote = p.token0, p.token1
    size = Decimal("0.5")

    dex_buy, dex_sell, _spot = eng.get_pair_prices_math(base, quote, size)

    base_atoms = int(size * Decimal(10**base.decimals))
    quote_in_atoms = p.get_amount_in(base_atoms, base)
    quote_out_atoms = p.get_amount_out(base_atoms, base)
    expected_buy = Decimal(quote_in_atoms) / Decimal(10**quote.decimals) / size
    expected_sell = Decimal(quote_out_atoms) / Decimal(10**quote.decimals) / size

    assert dex_buy == expected_buy
    assert dex_sell == expected_sell


def test_get_pair_prices_math_uses_pool_quote_decimals_not_caller_token() -> None:
    """``get_pair_prices_math`` must scale atoms with the pool's quote token decimals."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p])
    base = p.token0
    bad_quote = _tok(A2, "USDC", 18)  # same address as pool USDC; wrong decimals field
    good_buy, good_sell, good_spot = eng.get_pair_prices_math(base, p.token1, Decimal("1"))
    bad_buy, bad_sell, bad_spot = eng.get_pair_prices_math(base, bad_quote, Decimal("1"))
    assert bad_spot == good_spot
    assert bad_buy == good_buy
    assert bad_sell == good_sell
    eng = PricingEngine(MagicMock(), "http://x", "ws://x", SENDER)
    base = _tok(A1, "WETH")
    quote = _tok(A2, "USDC", 6)
    with pytest.raises(QuoteError, match="No pools loaded"):
        eng.get_pair_prices_math(base, quote, Decimal("1"))


def test_get_pair_prices_math_raises_on_zero_size() -> None:
    p = _pair()
    eng = _make_engine_with_pools([p])
    with pytest.raises(QuoteError, match="must be positive"):
        eng.get_pair_prices_math(p.token0, p.token1, Decimal("0"))


def test_get_pair_prices_math_resolves_eth_to_weth_pool() -> None:
    """Resolver must accept CEX-style ETH symbol against a WETH pool."""
    p = _pair(r0=10 * 10**18, r1=20_000 * 10**6)
    eng = _make_engine_with_pools([p])
    # Caller passes a pseudo-Token at a different address with symbol "ETH";
    # the engine should match it to the WETH side via symbol_match.
    eth_like = Token(
        address=Address("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
        symbol="ETH",
        decimals=18,
    )
    usdc = p.token1

    dex_buy, dex_sell, _spot = eng.get_pair_prices_math(eth_like, usdc, Decimal("0.1"))

    assert dex_sell < dex_buy
    assert dex_sell > 0


class _StubV3Pool:
    """Minimal V3 pool stub matching ``UniswapV3PoolQuoter``'s public surface."""

    def __init__(
        self,
        addr: Address,
        token0: Token,
        token1: Token,
        fee: int,
        rate_quote_per_base: Decimal,
    ) -> None:
        self.address = addr
        self.token0 = token0
        self.token1 = token1
        self.fee = fee
        self._rate = rate_quote_per_base

    def pool_id(self) -> str:
        return f"{self.address.lower}:{self.fee}"

    def quote_exact_input(self, token_in: Token, amount_in: int):
        from pricing.liquidity_pool import QuoteResult

        if token_in == self.token0:
            in_dec, out_dec = self.token0.decimals, self.token1.decimals
            rate = self._rate
        elif token_in == self.token1:
            in_dec, out_dec = self.token1.decimals, self.token0.decimals
            rate = Decimal(1) / self._rate
        else:
            return QuoteResult(amount_out=0, gas_estimate=0)
        in_human = Decimal(amount_in) / (Decimal(10) ** in_dec)
        out_human = in_human * rate
        atoms = int(out_human * (Decimal(10) ** out_dec))
        return QuoteResult(amount_out=atoms, gas_estimate=120_000)


def test_v2_only_path_unchanged_when_v3_pools_dict_empty() -> None:
    """With no V3 pools loaded, results equal the legacy V2 math byte-for-byte."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p])
    assert eng.v3_pools == {}
    legacy = eng.get_pair_prices_math(p.token0, p.token1, Decimal("1"))
    eng_with_pool = eng.get_pair_prices_math_with_pool(p.token0, p.token1, Decimal("1"))
    assert legacy == eng_with_pool[:3]
    assert eng_with_pool[4] == "v2"


def test_v3_pool_used_when_v2_absent() -> None:
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    v3 = _StubV3Pool(PAIR, weth, usdc, fee=500, rate_quote_per_base=Decimal("2000"))
    eng = PricingEngine(MagicMock(), "http://x", "ws://x", SENDER)
    eng.v3_pools = {PAIR: v3}  # type: ignore[dict-item]
    eng._rebuild_route_finder()
    dex_buy, dex_sell, spot, pool, kind = eng.get_pair_prices_math_with_pool(
        weth, usdc, Decimal("1")
    )
    assert kind == "v3"
    assert pool is v3
    assert dex_sell > 0 and dex_buy > 0
    assert spot > 0


def test_v3_wins_over_v2_when_better_quote() -> None:
    """When the V3 fee tier beats V2 on dex_sell, the engine picks V3."""
    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)  # spot ~2000
    weth, usdc = p.token0, p.token1
    # V3 stub priced at 2200 quote/base — strictly better dex_sell.
    v3 = _StubV3Pool(PAIR2, weth, usdc, fee=500, rate_quote_per_base=Decimal("2200"))
    eng = _make_engine_with_pools([p])
    eng.v3_pools = {PAIR2: v3}  # type: ignore[dict-item]
    eng._rebuild_route_finder()
    _buy, sell, _spot, pool, kind = eng.get_pair_prices_math_with_pool(weth, usdc, Decimal("1"))
    assert kind == "v3"
    assert pool is v3
    # Selected sell must beat what V2 would have returned on its own.
    v2_sell = eng._v2_pair_prices(p, weth, usdc, Decimal("1"), 10**18)[1]
    assert sell > v2_sell


def test_v2_wins_over_v3_when_v3_quoter_fails() -> None:
    """A V3 pool that returns 0 must not displace a working V2 pool."""

    class _FailingV3(_StubV3Pool):
        def quote_exact_input(self, token_in, amount_in):  # type: ignore[override]
            from pricing.liquidity_pool import QuoteResult

            return QuoteResult(amount_out=0, gas_estimate=0)

    p = _pair(r0=100 * 10**18, r1=200_000 * 10**6)
    weth, usdc = p.token0, p.token1
    bad_v3 = _FailingV3(PAIR2, weth, usdc, fee=500, rate_quote_per_base=Decimal("9999"))
    eng = _make_engine_with_pools([p])
    eng.v3_pools = {PAIR2: bad_v3}  # type: ignore[dict-item]
    eng._rebuild_route_finder()
    _buy, _sell, _spot, pool, kind = eng.get_pair_prices_math_with_pool(weth, usdc, Decimal("1"))
    assert kind == "v2"
    assert pool is p


def test_load_pools_v3_calls_from_chain() -> None:
    client = MagicMock()
    eng = PricingEngine(client, "http://x", "ws://x", SENDER)
    weth = _tok(A1, "WETH")
    usdc = _tok(A2, "USDC", 6)
    fake = _StubV3Pool(PAIR, weth, usdc, fee=3000, rate_quote_per_base=Decimal("2000"))

    from pricing import pricing_engine as pe

    with patch.object(pe.UniswapV3PoolQuoter, "from_chain", return_value=fake) as fc:
        eng.load_pools_v3([PAIR])

    fc.assert_called_once_with(PAIR, client, quoter_address=None)
    assert eng.v3_pools[PAIR] is fake
    assert eng.route_finder is not None


def test_refresh_pool_v3_unknown_raises() -> None:
    eng = PricingEngine(MagicMock(), "http://x", "ws://x", SENDER)
    with pytest.raises(QuoteError, match="V3 pool not loaded"):
        eng.refresh_pool_v3(PAIR)


def test_refresh_pool_changes_math_quote() -> None:
    """Refreshing reserves must change the math-only quote: not frozen."""
    p_initial = _pair(PAIR, r0=100 * 10**18, r1=200_000 * 10**6)
    eng = _make_engine_with_pools([p_initial])
    base, quote = p_initial.token0, p_initial.token1
    size = Decimal("1")

    buy_before, sell_before, spot_before = eng.get_pair_prices_math(base, quote, size)

    # Simulate LPs adding USDC: same WETH reserve, more USDC -> price up.
    p_updated = _pair(PAIR, r0=100 * 10**18, r1=240_000 * 10**6)
    with patch.object(UniswapV2Pair, "from_chain", return_value=p_updated):
        eng.refresh_pool(PAIR)

    buy_after, sell_after, spot_after = eng.get_pair_prices_math(base, quote, size)

    assert buy_after > buy_before
    assert sell_after > sell_before
    assert spot_after > spot_before
