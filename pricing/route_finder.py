from __future__ import annotations

from collections.abc import Sequence

from core.types import Token
from pricing.gas_cost import gas_cost_in_output_token, gas_cost_wei
from pricing.liquidity_graph import build_adjacency, find_all_paths
from pricing.liquidity_pool import LiquidityPoolQuote, as_liquidity_quote, v2_pools_for_gas_pricing
from pricing.route import Route
from pricing.uniswap_v2_pair import UniswapV2Pair


class RouteFinder:
    """
    Finds optimal routes between tokens.
    """

    def __init__(self, pools: Sequence[LiquidityPoolQuote | UniswapV2Pair]):
        self.pools: list[LiquidityPoolQuote] = [as_liquidity_quote(p) for p in pools]
        self._v2_for_gas = v2_pools_for_gas_pricing(self.pools)
        self.graph = build_adjacency(self.pools)

    def find_all_routes(
        self,
        token_in: Token,
        token_out: Token,
        max_hops: int = 3,
    ) -> list[Route]:
        """
        Find all simple routes up to max_hops pools.
        """
        raw = find_all_paths(self.graph, token_in, token_out, max_hops)
        return [Route(pools=p, path=t) for p, t in raw]

    def find_best_route(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
        max_hops: int = 3,
        eth_price_in_output: int | None = None,
    ) -> tuple[Route | None, int]:
        """
        Route that maximizes net output (gross − gas priced in *token_out*).

        Returns ``(best_route, net_output)``. If no route exists, ``(None, 0)``.
        """
        routes = self.find_all_routes(token_in, token_out, max_hops=max_hops)
        if not routes:
            return None, 0

        best_route: Route | None = None
        best_net = -(10**36)
        for route in routes:
            gross = route.get_output(amount_in)
            gas_units = route.estimate_gas(amount_in)
            gas_wei = gas_cost_wei(gas_units, gas_price_gwei)
            gas_out = gas_cost_in_output_token(
                self._v2_for_gas, token_out, gas_wei, eth_price_in_output
            )
            net = gross - gas_out
            if net > best_net:
                best_net = net
                best_route = route
        return best_route, best_net

    def compare_routes(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
        max_hops: int = 3,
        eth_price_in_output: int | None = None,
    ) -> list[dict]:
        """
        Per-route breakdown. ``gas_cost`` is the output-token deduction (same units as
        ``gross_output`` / ``net_output``). ``gas_cost_wei`` is raw wei spent on gas.
        """
        rows: list[dict] = []
        for route in self.find_all_routes(token_in, token_out, max_hops=max_hops):
            gross = route.get_output(amount_in)
            gas_estimate = route.estimate_gas(amount_in)
            gas_wei = gas_cost_wei(gas_estimate, gas_price_gwei)
            gas_out = gas_cost_in_output_token(
                self._v2_for_gas, token_out, gas_wei, eth_price_in_output
            )
            rows.append(
                {
                    "route": route,
                    "gross_output": gross,
                    "gas_estimate": gas_estimate,
                    "gas_cost_wei": gas_wei,
                    "gas_cost": gas_out,
                    "net_output": gross - gas_out,
                }
            )
        rows.sort(key=lambda r: r["net_output"], reverse=True)
        return rows
