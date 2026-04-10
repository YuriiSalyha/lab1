"""Tests for Uniswap V2 pair math and :class:`pricing.price_impact_analyzer.PriceImpactAnalyzer`."""

from decimal import Decimal

import pytest

from core.types import Address, Token
from pricing.price_impact_analyzer import PriceImpactAnalyzer
from pricing.uniswap_v2_pair import UniswapV2Pair

# ---------------------------------------------------------------------------
# Test data (Ethereum mainnet)
# ---------------------------------------------------------------------------
# Uniswap V2 WETH/USDC — token0 = USDC, token1 = WETH
PAIR_ETH_USDC = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")
# Uniswap V2 WETH/USDT — token0 = USDT, token1 = WETH
PAIR_ETH_USDT = Address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852")

# Router swap WETH → ELON (single hop on pair below); receipt used for exact amounts.
TX_ETH_TO_ELON = "0x3d6b02d8bd4a866e979d9c9b706979182078f38aa6cfab5f19952538e33d6b98"
# Pair from Swap log; token0 = ELON, token1 = WETH (mainnet sort order).
PAIR_ELON_WETH = Address("0x7B73644935b8e68019aC6356c40661E1bc315860")
ELON_ADDR = Address("0x761D38e5ddf6ccf6Cf7c55759d5210750B5D60F3")

WETH_ADDR = Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
USDC_ADDR = Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
USDT_ADDR = Address("0xdAC17F958D2ee523a2206206994597C13D831ec7")
DAI_ADDR = Address("0x6B175474E89094C44Da98b954EedeAC495271d0F")

# Pre-swap reserves for PAIR_ELON_WETH, reconstructed from tx receipt Sync + Swap (block 24836089).
_ELON_RESERVE0 = 71_854_714_845_456_600_827_892_703_182_854
_WETH_RESERVE1 = 1_288_875_086_749_496_657_063
_WETH_IN_WEI = 1_021_021_953_129_200_000
_ELON_OUT = 56_706_364_776_981_923_919_291_626_376


def test_get_amount_out_basic() -> None:
    """1000 WETH / 2M USDC pool layout matching mainnet WETH/USDC pair ordering."""
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    pair = UniswapV2Pair(
        address=PAIR_ETH_USDC,
        token0=usdc,
        token1=weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
        fee_bps=30,
    )

    usdc_in = 2000 * 10**6
    eth_out = pair.get_amount_out(usdc_in, usdc)

    assert eth_out == 996_006_981_039_903_216
    assert eth_out < 1 * 10**18
    assert eth_out > 99 * 10**16


def test_get_amount_out_matches_solidity() -> None:
    """
    On-chain vector from router tx ``TX_ETH_TO_ELON`` (WETH in, ELON out).

    Reserves are the pair state immediately before that swap, derived from the
    receipt (Sync + Swap) so no archive RPC is required in CI.
    """
    elon = Token(address=ELON_ADDR, symbol="ELON", decimals=9)
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    pair = UniswapV2Pair(
        address=PAIR_ELON_WETH,
        token0=elon,
        token1=weth,
        reserve0=_ELON_RESERVE0,
        reserve1=_WETH_RESERVE1,
        fee_bps=30,
    )

    elon_out = pair.get_amount_out(_WETH_IN_WEI, weth)
    assert elon_out == _ELON_OUT


def test_integer_math_no_floats() -> None:
    """Large reserves; output stays an exact int (no floats in assertions)."""
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    pair = UniswapV2Pair(
        address=PAIR_ETH_USDC,
        token0=usdc,
        token1=weth,
        reserve0=10**30,
        reserve1=10**30,
        fee_bps=30,
    )
    out = pair.get_amount_out(10**25, weth)
    assert isinstance(out, int)
    assert out > 0


def test_swap_is_immutable() -> None:
    """``simulate_swap`` does not mutate the original pair instance."""
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    pair = UniswapV2Pair(
        address=PAIR_ETH_USDC,
        token0=usdc,
        token1=weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
        fee_bps=30,
    )
    amount = 5 * 10**17
    original_r0 = pair.reserve0
    original_r1 = pair.reserve1
    new_pair = pair.simulate_swap(amount, weth)

    assert pair.reserve0 == original_r0
    assert pair.reserve1 == original_r1
    assert new_pair.reserve1 == original_r1 + amount
    assert new_pair.reserve0 < original_r0


def test_eth_usdt_pair_mainnet_address_and_ordering() -> None:
    """Reference pool from test data: USDT is token0, WETH is token1."""
    weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
    usdt = Token(address=USDT_ADDR, symbol="USDT", decimals=6)
    pair = UniswapV2Pair(
        address=PAIR_ETH_USDT,
        token0=usdt,
        token1=weth,
        reserve0=1_000_000 * 10**6,
        reserve1=500 * 10**18,
        fee_bps=30,
    )
    out = pair.get_amount_out(10**18, weth)
    assert isinstance(out, int)
    assert out > 0


# ---------------------------------------------------------------------------
# PriceImpactAnalyzer
# ---------------------------------------------------------------------------


def _weth_usdc_pair() -> UniswapV2Pair:
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


def _dai_usdc_pair_no_eth() -> UniswapV2Pair:
    dai = Token(address=DAI_ADDR, symbol="DAI", decimals=18)
    usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
    return UniswapV2Pair(
        address=Address("0x0000000000000000000000000000000000000001"),
        token0=dai,
        token1=usdc,
        reserve0=10**24,
        reserve1=10**12,
        fee_bps=30,
    )


@pytest.fixture
def analyzer() -> PriceImpactAnalyzer:
    return PriceImpactAnalyzer(_weth_usdc_pair())


def test_generate_impact_table_rows(analyzer: PriceImpactAnalyzer) -> None:
    weth = analyzer.pair.token1
    rows = analyzer.generate_impact_table(weth, [10**16, 10**17])
    assert len(rows) == 2
    assert rows[0]["amount_in"] == 10**16
    assert isinstance(rows[0]["amount_out"], int)
    assert isinstance(rows[0]["spot_price"], Decimal)
    assert isinstance(rows[0]["execution_price"], Decimal)
    assert isinstance(rows[0]["price_impact_pct"], Decimal)


def test_generate_impact_table_rejects_non_positive(analyzer: PriceImpactAnalyzer) -> None:
    weth = analyzer.pair.token1
    with pytest.raises(ValueError, match="greater than 0"):
        analyzer.generate_impact_table(weth, [0, 1])


def test_find_max_size_for_impact_respects_bound(analyzer: PriceImpactAnalyzer) -> None:
    weth = analyzer.pair.token1
    max_pct = Decimal("5")
    best = analyzer.find_max_size_for_impact(weth, max_pct)
    assert isinstance(best, int)
    assert best >= 0
    limit = max_pct / Decimal("100")
    if best > 0:
        assert analyzer.pair.get_price_impact(best, weth) <= limit


def test_estimate_true_cost_weth_in_sets_gas_cost_eth() -> None:
    analyzer = PriceImpactAnalyzer(_weth_usdc_pair())
    weth = analyzer.pair.token1
    gas_price_gwei = 30
    gas_estimate = 150_000
    amount_in = 10**18
    r = analyzer.estimate_true_cost(amount_in, weth, gas_price_gwei, gas_estimate)
    expected_wei = gas_price_gwei * gas_estimate * 10**9
    assert r["gas_cost_eth"] == expected_wei
    assert r["gross_output"] == analyzer.pair.get_amount_out(amount_in, weth)
    assert (
        r["gas_cost_in_output_token"]
        == expected_wei * analyzer.pair.reserve0 // analyzer.pair.reserve1
    )
    assert r["net_output"] == r["gross_output"] - r["gas_cost_in_output_token"]


def test_estimate_true_cost_usdc_in_gas_subtracted_in_weth_units() -> None:
    analyzer = PriceImpactAnalyzer(_weth_usdc_pair())
    usdc = analyzer.pair.token0
    gas_price_gwei = 30
    gas_estimate = 150_000
    amount_in = 500_000 * 10**6
    r = analyzer.estimate_true_cost(amount_in, usdc, gas_price_gwei, gas_estimate)
    expected_wei = gas_price_gwei * gas_estimate * 10**9
    assert r["gas_cost_eth"] == expected_wei
    assert r["gas_cost_in_output_token"] == expected_wei
    assert r["net_output"] == r["gross_output"] - expected_wei


def test_estimate_true_cost_non_eth_pair_requires_or_accepts_price() -> None:
    analyzer = PriceImpactAnalyzer(_dai_usdc_pair_no_eth())
    dai = analyzer.pair.token0
    gas_price_gwei = 1
    gas_estimate = 21_000
    with pytest.raises(ValueError, match="Neither pair token is ETH"):
        analyzer.estimate_true_cost(10**18, dai, gas_price_gwei, gas_estimate)

    eth_price_in_output = 3000 * 10**6
    r = analyzer.estimate_true_cost(
        10**18,
        dai,
        gas_price_gwei,
        gas_estimate,
        eth_price_in_output=eth_price_in_output,
    )
    gas_wei = gas_price_gwei * gas_estimate * 10**9
    expected_gas_usdc = gas_wei * eth_price_in_output // 10**18
    assert r["gas_cost_in_output_token"] == expected_gas_usdc


def test_estimate_true_cost_explicit_price_overrides_pair_detection() -> None:
    analyzer = PriceImpactAnalyzer(_weth_usdc_pair())
    weth = analyzer.pair.token1
    gas_price_gwei = 10
    gas_estimate = 100_000
    gas_wei = gas_price_gwei * gas_estimate * 10**9
    custom_price = 5000 * 10**6
    r = analyzer.estimate_true_cost(
        10**18,
        weth,
        gas_price_gwei,
        gas_estimate,
        eth_price_in_output=custom_price,
    )
    assert r["gas_cost_in_output_token"] == gas_wei * custom_price // 10**18


def test_estimate_true_cost_rejects_bad_amount(analyzer: PriceImpactAnalyzer) -> None:
    weth = analyzer.pair.token1
    with pytest.raises(ValueError, match="greater than 0"):
        analyzer.estimate_true_cost(0, weth, 1, 21_000)
