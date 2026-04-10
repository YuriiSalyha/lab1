from core.types import Token

from .uniswap_v2_pair import UniswapV2Pair

# Uniswap-style router gas heuristic: direct ~150k, each extra hop ~+100k.
_BASE_SWAP_GAS = 150_000
_GAS_PER_EXTRA_HOP = 100_000


class Route:
    """Represents a swap route through one or more pools."""

    def __init__(self, pools: list[UniswapV2Pair], path: list[Token]):
        self.pools = pools
        self.path = path  # token_in → intermediate... → token_out
        self._validate()

    def _validate(self) -> None:
        if len(self.pools) + 1 != len(self.path):
            raise ValueError(
                f"path must have len(pools)+1 tokens, got {len(self.path)} tokens "
                f"for {len(self.pools)} pools"
            )
        for i, pool in enumerate(self.pools):
            a, b = self.path[i], self.path[i + 1]
            if a == b:
                raise ValueError("path must not contain a zero-length hop")
            if {a, b} != {pool.token0, pool.token1}:
                raise ValueError(
                    f"pool {i} does not connect {a!r} → {b!r} "
                    f"(pair has {pool.token0!r} / {pool.token1!r})"
                )

    @property
    def num_hops(self) -> int:
        return len(self.pools)

    def token_in(self) -> Token:
        return self.path[0]

    def token_out(self) -> Token:
        return self.path[-1]

    def get_output(self, amount_in: int) -> int:
        """Simulate full route, return final output."""
        if amount_in <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in}")
        amount = amount_in
        for i, pool in enumerate(self.pools):
            t = self.path[i]
            amount = pool.get_amount_out(amount, t)
        return amount

    def get_intermediate_amounts(self, amount_in: int) -> list[int]:
        """Return amount at each step: [input, after_hop1, after_hop2, ...]"""
        if amount_in <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in}")
        amounts: list[int] = [amount_in]
        cur = amount_in
        for i, pool in enumerate(self.pools):
            t = self.path[i]
            cur = pool.get_amount_out(cur, t)
            amounts.append(cur)
        return amounts

    def estimate_gas(self) -> int:
        """Estimate gas: ~150k base + ~100k per additional hop."""
        n = self.num_hops
        if n <= 0:
            return 0
        return _BASE_SWAP_GAS + _GAS_PER_EXTRA_HOP * (n - 1)
