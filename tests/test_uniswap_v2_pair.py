"""Extra unit tests for :class:`pricing.uniswap_v2_pair.UniswapV2Pair`."""

from decimal import Decimal

import pytest

from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair

# Mainnet WETH/USDC: token0 = USDC, token1 = WETH (same layout as ``test_aam_pricer``).
PAIR_ETH_USDC = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")
WETH_ADDR = Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
USDC_ADDR = Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
DAI_ADDR = Address("0x6B175474E89094C44Da98b954EedeAC495271d0F")


@pytest.fixture
def weth_usdc_pair() -> UniswapV2Pair:
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    return UniswapV2Pair(
        address=PAIR_ETH_USDC,
        token0=usdc,
        token1=weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
        fee_bps=30,
    )


def test_get_amount_out_weth_to_usdc(weth_usdc_pair: UniswapV2Pair) -> None:
    weth = weth_usdc_pair.token1
    weth_in = 1 * 10**18
    usdc_out = weth_usdc_pair.get_amount_out(weth_in, weth)
    assert isinstance(usdc_out, int)
    assert usdc_out > 0
    assert 1_900 * 10**6 < usdc_out < 2_100 * 10**6


def test_get_amount_out_rejects_unknown_token(weth_usdc_pair: UniswapV2Pair) -> None:
    other = Token(address=DAI_ADDR, symbol="DAI", decimals=18)
    with pytest.raises(ValueError, match="not a valid token"):
        weth_usdc_pair.get_amount_out(10**18, other)


def test_get_amount_in_inverse_of_get_amount_out(weth_usdc_pair: UniswapV2Pair) -> None:
    weth = weth_usdc_pair.token1
    usdc = weth_usdc_pair.token0
    target_usdc_out = 50_000 * 10**6
    weth_in = weth_usdc_pair.get_amount_in(target_usdc_out, usdc)
    assert weth_in > 0
    actual_out = weth_usdc_pair.get_amount_out(weth_in, weth)
    assert actual_out >= target_usdc_out - 1


def test_get_amount_in_rejects_unknown_token(weth_usdc_pair: UniswapV2Pair) -> None:
    other = Token(address=DAI_ADDR, symbol="DAI", decimals=18)
    with pytest.raises(ValueError, match="not a valid token"):
        weth_usdc_pair.get_amount_in(10**6, other)


def test_get_spot_and_execution_price_use_decimals_only(weth_usdc_pair: UniswapV2Pair) -> None:
    weth = weth_usdc_pair.token1
    spot = weth_usdc_pair.get_spot_price(weth)
    assert isinstance(spot, Decimal)
    exec_px = weth_usdc_pair.get_execution_price(10**18, weth)
    assert isinstance(exec_px, Decimal)


def test_get_price_impact_worse_for_large_sell(weth_usdc_pair: UniswapV2Pair) -> None:
    weth = weth_usdc_pair.token1
    small = 10**15
    large = 100 * 10**18
    impact_small = weth_usdc_pair.get_price_impact(small, weth)
    impact_large = weth_usdc_pair.get_price_impact(large, weth)
    assert impact_small < 0 and impact_large < 0
    assert impact_large < impact_small


def test_simulate_swap_token0_in_updates_correct_side(weth_usdc_pair: UniswapV2Pair) -> None:
    usdc = weth_usdc_pair.token0
    amount_in = 10_000 * 10**6
    new_pair = weth_usdc_pair.simulate_swap(amount_in, usdc)
    assert new_pair.reserve0 == weth_usdc_pair.reserve0 + amount_in
    assert new_pair.reserve1 < weth_usdc_pair.reserve1


def test_simulate_swap_increases_product(weth_usdc_pair: UniswapV2Pair) -> None:
    weth = weth_usdc_pair.token1
    k_before = weth_usdc_pair.reserve0 * weth_usdc_pair.reserve1
    new_pair = weth_usdc_pair.simulate_swap(10**18, weth)
    k_after = new_pair.reserve0 * new_pair.reserve1
    assert k_after >= k_before


def test_fee_bps_zero_maximizes_output_vs_default_fee() -> None:
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    base = dict(
        address=PAIR_ETH_USDC,
        token0=usdc,
        token1=weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
    )
    no_fee = UniswapV2Pair(**base, fee_bps=0)
    with_fee = UniswapV2Pair(**base, fee_bps=30)
    weth_in = 10**18
    assert no_fee.get_amount_out(weth_in, weth) > with_fee.get_amount_out(weth_in, weth)


def test_from_subgraph_row_builds_pair() -> None:
    row = {
        "id": PAIR_ETH_USDC.checksum,
        "reserve0": str(2_000_000 * 10**6),
        "reserve1": str(1000 * 10**18),
        "token0": {
            "id": USDC_ADDR.checksum,
            "symbol": "USDC",
            "decimals": 6,
        },
        "token1": {
            "id": WETH_ADDR.checksum,
            "symbol": "WETH",
            "decimals": 18,
        },
    }
    p = UniswapV2Pair.from_subgraph_row(row)
    assert p is not None
    assert p.address == PAIR_ETH_USDC
    assert p.token0.symbol == "USDC"
    assert p.reserve0 == 2_000_000 * 10**6


def test_from_subgraph_row_rejects_zero_reserves() -> None:
    row = {
        "id": "0x0000000000000000000000000000000000000001",
        "reserve0": "0",
        "reserve1": "1",
        "token0": {"id": USDC_ADDR.checksum, "symbol": "USDC", "decimals": 6},
        "token1": {"id": WETH_ADDR.checksum, "symbol": "WETH", "decimals": 18},
    }
    assert UniswapV2Pair.from_subgraph_row(row) is None
