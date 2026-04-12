"""Convert swap-route gas (wei) into ERC-20 output units using V2 WETH pools."""

from __future__ import annotations

from core.types import Token
from pricing.uniswap_v2_pair import UniswapV2Pair

_ETH_SYMBOLS = frozenset({"ETH", "WETH", "wETH"})


def gas_cost_wei(gas_estimate: int, gas_price_gwei: int) -> int:
    return gas_estimate * gas_price_gwei * 10**9


def gas_cost_in_output_token(
    v2_pools: list[UniswapV2Pair],
    token_out: Token,
    gas_wei: int,
    eth_price_in_output: int | None,
) -> int:
    """Convert gas (wei) to *token_out* raw units using a WETH pair or explicit ETH price."""
    if token_out.symbol in _ETH_SYMBOLS:
        return gas_wei
    for pool in v2_pools:
        t0, t1 = pool.token0, pool.token1
        if token_out == t0 and t1.symbol in _ETH_SYMBOLS:
            return gas_wei * pool.reserve0 // pool.reserve1
        if token_out == t1 and t0.symbol in _ETH_SYMBOLS:
            return gas_wei * pool.reserve1 // pool.reserve0
    if eth_price_in_output is not None:
        return gas_wei * eth_price_in_output // 10**18
    raise ValueError(
        "Cannot convert gas to output token: no pool with WETH/ETH and token_out, "
        "and eth_price_in_output was not provided (raw output per 10**18 wei)"
    )
