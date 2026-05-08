"""Convert swap-route gas (wei) into ERC-20 output units using V2 or V3 WETH pools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.types import Token
from pricing.uniswap_v2_pair import UniswapV2Pair

if TYPE_CHECKING:
    from pricing.uniswap_v3_pool import UniswapV3PoolQuoter

_ETH_SYMBOLS = frozenset({"ETH", "WETH", "wETH"})


def gas_cost_wei(gas_estimate: int, gas_price_gwei: int) -> int:
    return gas_estimate * gas_price_gwei * 10**9


def gas_cost_in_output_token(
    v2_pools: list[UniswapV2Pair],
    token_out: Token,
    gas_wei: int,
    eth_price_in_output: int | None,
    v3_pools: "list[UniswapV3PoolQuoter] | None" = None,
) -> int:
    """Convert gas (wei) to *token_out* raw units using a WETH pair or explicit ETH price.

    Pure-V2 deployments (``v3_pools`` defaulting to ``None``) keep the legacy
    behavior. When provided, V3 pools paired with WETH are queried via QuoterV2
    only as a *fallback* — i.e. when no V2 WETH pool exists for ``token_out``
    and ``eth_price_in_output`` was not supplied. This avoids extra ``eth_call``
    traffic on the hot path of V2-only environments.
    """
    if token_out.symbol in _ETH_SYMBOLS:
        return gas_wei
    for pool in v2_pools:
        t0, t1 = pool.token0, pool.token1
        if token_out == t0 and t1.symbol in _ETH_SYMBOLS:
            return gas_wei * pool.reserve0 // pool.reserve1
        if token_out == t1 and t0.symbol in _ETH_SYMBOLS:
            return gas_wei * pool.reserve1 // pool.reserve0
    if v3_pools:
        for pool in v3_pools:
            t0, t1 = pool.token0, pool.token1
            if token_out == t0 and t1.symbol in _ETH_SYMBOLS:
                weth_token = t1
            elif token_out == t1 and t0.symbol in _ETH_SYMBOLS:
                weth_token = t0
            else:
                continue
            try:
                # 1 ETH (in atoms) -> token_out atoms via QuoterV2; gives output-per-ETH.
                eth_in_atoms = 10**weth_token.decimals
                qr = pool.quote_exact_input(weth_token, eth_in_atoms)
                if qr.amount_out > 0:
                    return gas_wei * qr.amount_out // 10**18
            except Exception:
                continue
    if eth_price_in_output is not None:
        return gas_wei * eth_price_in_output // 10**18
    raise ValueError(
        "Cannot convert gas to output token: no pool with WETH/ETH and token_out, "
        "and eth_price_in_output was not provided (raw output per 10**18 wei)"
    )
