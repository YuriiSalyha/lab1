from __future__ import annotations

from core.types import Address, Token

from .route import Route
from .uniswap_v2_pair import UniswapV2Pair

_ETH_SYMBOLS = frozenset({"ETH", "WETH", "wETH"})


def _gas_cost_wei(gas_estimate: int, gas_price_gwei: int) -> int:
    return gas_estimate * gas_price_gwei * 10**9


def _gas_cost_in_output_token(
    all_pools: list[UniswapV2Pair],
    token_out: Token,
    gas_wei: int,
    eth_price_in_output: int | None,
) -> int:
    """Convert gas (wei) to *token_out* raw units using a WETH pair or explicit ETH price."""
    if token_out.symbol in _ETH_SYMBOLS:
        return gas_wei
    for pool in all_pools:
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


class RouteFinder:
    """
    Finds optimal routes between tokens.
    """

    def __init__(self, pools: list[UniswapV2Pair]):
        self.pools = pools
        self.graph = self._build_graph()

    def _build_graph(self) -> dict[Token, list[tuple[UniswapV2Pair, Token]]]:
        """Adjacency: token → [(pool, other_token), ...]."""
        graph: dict[Token, list[tuple[UniswapV2Pair, Token]]] = {}
        for pool in self.pools:
            t0, t1 = pool.token0, pool.token1
            graph.setdefault(t0, []).append((pool, t1))
            graph.setdefault(t1, []).append((pool, t0))
        return graph

    def find_all_routes(
        self,
        token_in: Token,
        token_out: Token,
        max_hops: int = 3,
    ) -> list[Route]:
        """
        Find all simple routes up to max_hops pools.
        """
        if token_in == token_out:
            return []
        if max_hops < 1:
            return []

        routes: list[Route] = []
        used_pool_addrs: set[Address] = set()

        def dfs(
            current: Token,
            path_tokens: list[Token],
            pools_so_far: list[UniswapV2Pair],
            hop_count: int,
        ) -> None:
            if current == token_out and hop_count > 0:
                routes.append(Route(pools=list(pools_so_far), path=list(path_tokens)))
                return
            if hop_count >= max_hops:
                return
            for pool, nxt in self.graph.get(current, []):
                addr = pool.address
                if addr in used_pool_addrs:
                    continue
                if nxt in path_tokens:
                    continue
                used_pool_addrs.add(addr)
                path_tokens.append(nxt)
                pools_so_far.append(pool)
                dfs(nxt, path_tokens, pools_so_far, hop_count + 1)
                pools_so_far.pop()
                path_tokens.pop()
                used_pool_addrs.remove(addr)

        dfs(token_in, [token_in], [], 0)
        return routes

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
            gas_units = route.estimate_gas()
            gas_wei = _gas_cost_wei(gas_units, gas_price_gwei)
            gas_out = _gas_cost_in_output_token(self.pools, token_out, gas_wei, eth_price_in_output)
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
            gas_estimate = route.estimate_gas()
            gas_wei = _gas_cost_wei(gas_estimate, gas_price_gwei)
            gas_out = _gas_cost_in_output_token(self.pools, token_out, gas_wei, eth_price_in_output)
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
