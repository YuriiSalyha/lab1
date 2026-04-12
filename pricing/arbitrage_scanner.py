"""Cycle arbitrage scanner over a loaded pool set (V2 / V3 quotes via :class:`Route`)."""

from __future__ import annotations

from collections.abc import Sequence

from pricing.arbitrage_types import ArbitrageOpportunity
from pricing.batch_quote import BatchQuoteExecutor
from pricing.gas_cost import gas_cost_in_output_token, gas_cost_wei
from pricing.liquidity_graph import build_adjacency, find_simple_cycles
from pricing.liquidity_pool import LiquidityPoolQuote, as_liquidity_quote, v2_pools_for_gas_pricing
from pricing.route import Route
from pricing.route_batch_quote import batch_quote_route_outputs
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter


def default_amount_grid(*, max_raw: int, steps: int = 16) -> list[int]:
    """Powers of two up to ``max_raw``, capped, deduped, sorted."""
    if max_raw <= 0:
        return []
    out: list[int] = []
    x = 1
    while x <= max_raw and len(out) < steps:
        out.append(x)
        if x > max_raw // 2:
            break
        x *= 2
    if max_raw not in out:
        out.append(max_raw)
    return sorted(set(out))


def _profit_bps(profit_raw: int, amount_in: int) -> int:
    if amount_in <= 0:
        return 0
    return (profit_raw * 10_000) // amount_in


class ArbitrageScanner:
    """
    Finds simple cyclic routes where ``route.get_output(amount_in) > amount_in`` for some grid
    of ``amount_in`` values.

    Without ``batch_executor``: uses :class:`Route.get_output` (fine for pure V2).

    With ``batch_executor``: V3 hops are quoted via Multicall (one RPC per hop wave per chunk);
    V2 hops stay local. Summed hop ``gas_estimate`` (Quoter on V3) is used for net profit.
    """

    def __init__(
        self,
        pools: Sequence[LiquidityPoolQuote | UniswapV2Pair],
        *,
        max_cycle_len: int = 3,
        gas_price_gwei: int = 0,
        batch_executor: BatchQuoteExecutor | None = None,
        batch_chunk: int = 200,
    ) -> None:
        if max_cycle_len < 2 or max_cycle_len > 4:
            raise ValueError("max_cycle_len must be between 2 and 4")
        if batch_chunk < 1:
            raise ValueError("batch_chunk must be >= 1")
        self.pools: list[LiquidityPoolQuote] = [as_liquidity_quote(p) for p in pools]
        self._v2_for_gas = v2_pools_for_gas_pricing(self.pools)
        self.graph = build_adjacency(self.pools)
        self.max_cycle_len = max_cycle_len
        self.gas_price_gwei = gas_price_gwei
        self._batch_executor = batch_executor
        self._batch_chunk = batch_chunk

    def find_opportunities(
        self,
        *,
        amount_candidates: Sequence[int] | None = None,
        max_raw: int = 10**18,
    ) -> list[ArbitrageOpportunity]:
        """
        Evaluate all simple cycles (up to ``max_cycle_len`` pools) on the grid.

        If ``amount_candidates`` is None, uses :func:`default_amount_grid` with ``max_raw``.
        """
        cycles = find_simple_cycles(self.graph, max_cycle_len=self.max_cycle_len)
        if amount_candidates is not None:
            grid = list(amount_candidates)
        else:
            grid = default_amount_grid(max_raw=max_raw)
        opps: list[ArbitrageOpportunity] = []

        for pool_list, path in cycles:
            route = Route(pools=list(pool_list), path=list(path))
            start = path[0]
            has_v3 = any(isinstance(p, UniswapV3PoolQuoter) for p in route.pools)

            if self._batch_executor is not None and has_v3:
                batched = batch_quote_route_outputs(
                    route,
                    grid,
                    self._batch_executor,
                    chunk_size=self._batch_chunk,
                )
                for amt, out_amt, gas_units in batched:
                    if out_amt is None:
                        continue
                    gas_wei = gas_cost_wei(gas_units, self.gas_price_gwei)
                    try:
                        gas_start = gas_cost_in_output_token(
                            self._v2_for_gas, start, gas_wei, eth_price_in_output=None
                        )
                    except ValueError:
                        gas_start = 0
                    profit_raw = out_amt - amt
                    if profit_raw <= 0:
                        continue
                    profit_net = profit_raw - gas_start
                    opps.append(
                        ArbitrageOpportunity(
                            pools=tuple(pool_list),
                            path=tuple(path),
                            amount_in=amt,
                            amount_out=out_amt,
                            profit_raw=profit_raw,
                            profit_bps=_profit_bps(profit_raw, amt),
                            gas_estimate=gas_units,
                            gas_cost_start_token=gas_start,
                            profit_net=profit_net,
                        )
                    )
                continue

            for amt in grid:
                if amt <= 0:
                    continue
                try:
                    out_amt = route.get_output(amt)
                    gas_units = route.estimate_gas(amt)
                except (ValueError, ZeroDivisionError):
                    continue
                gas_wei = gas_cost_wei(gas_units, self.gas_price_gwei)
                try:
                    gas_start = gas_cost_in_output_token(
                        self._v2_for_gas, start, gas_wei, eth_price_in_output=None
                    )
                except ValueError:
                    gas_start = 0
                profit_raw = out_amt - amt
                if profit_raw <= 0:
                    continue
                profit_net = profit_raw - gas_start
                opps.append(
                    ArbitrageOpportunity(
                        pools=tuple(pool_list),
                        path=tuple(path),
                        amount_in=amt,
                        amount_out=out_amt,
                        profit_raw=profit_raw,
                        profit_bps=_profit_bps(profit_raw, amt),
                        gas_estimate=gas_units,
                        gas_cost_start_token=gas_start,
                        profit_net=profit_net,
                    )
                )

        opps.sort(key=lambda o: o.profit_net, reverse=True)
        return opps
