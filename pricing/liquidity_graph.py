"""
Shared token–pool adjacency and DFS for routing and cycle (arb) search.

**Dense graphs:** mixing many V2 and V3 pools on the same token set grows edges quickly; the
number of simple cycles grows combinatorially with pool count. Keep ``max_cycle_len`` at 3
(default) for scans; use 4 only on small pool sets. Hub filtering (e.g. WETH/USDC-only pools) is
a later optimization.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.types import Token
from pricing.liquidity_pool import LiquidityPoolQuote


def build_adjacency(
    pools: Sequence[LiquidityPoolQuote],
) -> dict[Token, list[tuple[LiquidityPoolQuote, Token]]]:
    graph: dict[Token, list[tuple[LiquidityPoolQuote, Token]]] = {}
    for pool in pools:
        t0, t1 = pool.token0, pool.token1
        graph.setdefault(t0, []).append((pool, t1))
        graph.setdefault(t1, []).append((pool, t0))
    return graph


def find_all_paths(
    graph: dict[Token, list[tuple[LiquidityPoolQuote, Token]]],
    token_in: Token,
    token_out: Token,
    max_hops: int,
) -> list[tuple[list[LiquidityPoolQuote], list[Token]]]:
    """Simple paths from ``token_in`` to ``token_out`` (no repeated pool or token on path)."""
    if token_in == token_out or max_hops < 1:
        return []

    results: list[tuple[list[LiquidityPoolQuote], list[Token]]] = []
    used_pool_ids: set[str] = set()

    def dfs(
        current: Token,
        path_tokens: list[Token],
        pools_so_far: list[LiquidityPoolQuote],
        hop_count: int,
    ) -> None:
        if current == token_out and hop_count > 0:
            results.append((list(pools_so_far), list(path_tokens)))
            return
        if hop_count >= max_hops:
            return
        for pool, nxt in graph.get(current, []):
            pid = pool.pool_id()
            if pid in used_pool_ids or nxt in path_tokens:
                continue
            used_pool_ids.add(pid)
            path_tokens.append(nxt)
            pools_so_far.append(pool)
            dfs(nxt, path_tokens, pools_so_far, hop_count + 1)
            pools_so_far.pop()
            path_tokens.pop()
            used_pool_ids.remove(pid)

    dfs(token_in, [token_in], [], 0)
    return results


def find_simple_cycles(
    graph: dict[Token, list[tuple[LiquidityPoolQuote, Token]]],
    *,
    max_cycle_len: int,
) -> list[tuple[list[LiquidityPoolQuote], list[Token]]]:
    """
    Simple cycles (no repeated pool) that return to the start token.

    ``max_cycle_len`` is the maximum number of **pools** in the cycle.
    Each result is ``(pools, path)`` with ``path[0] == path[-1]`` and
    ``len(path) == len(pools) + 1``.
    The same cyclic set of pools is only returned once (unordered pool-id key).

    On large pool graphs, the number of cycles can explode; cap ``max_cycle_len`` and pool count.
    """
    if max_cycle_len < 2:
        return []

    cycles: list[tuple[list[LiquidityPoolQuote], list[Token]]] = []
    seen_cycle_keys: set[frozenset[str]] = set()

    for start in graph:
        used_pool_ids: set[str] = set()

        def dfs(
            current: Token,
            path_tokens: list[Token],
            pools_so_far: list[LiquidityPoolQuote],
            *,
            cycle_start: Token = start,
            pool_ids: set[str] = used_pool_ids,
        ) -> None:
            for pool, nxt in graph.get(current, []):
                pid = pool.pool_id()
                if pid in pool_ids:
                    continue

                if nxt == cycle_start and len(pools_so_far) >= 1:
                    full_pools = pools_so_far + [pool]
                    if len(full_pools) <= max_cycle_len:
                        key = frozenset(p.pool_id() for p in full_pools)
                        if key not in seen_cycle_keys:
                            seen_cycle_keys.add(key)
                            cycles.append((full_pools, path_tokens + [cycle_start]))
                    continue

                if nxt in path_tokens:
                    continue

                pool_ids.add(pid)
                path_tokens.append(nxt)
                pools_so_far.append(pool)
                dfs(nxt, path_tokens, pools_so_far)
                pools_so_far.pop()
                path_tokens.pop()
                pool_ids.remove(pid)

        dfs(start, [start], [])

    return cycles
