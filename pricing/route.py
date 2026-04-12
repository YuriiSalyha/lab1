from collections.abc import Sequence

from core.types import Token

from .liquidity_pool import LiquidityPoolQuote, QuoteResult, as_liquidity_quote
from .uniswap_v2_pair import UniswapV2Pair

# Uniswap-style router gas heuristic: direct ~150k, each extra hop ~+100k.
_BASE_SWAP_GAS = 150_000
_GAS_PER_EXTRA_HOP = 100_000


class Route:
    """Represents a swap route through one or more pools."""

    def __init__(
        self,
        pools: Sequence[LiquidityPoolQuote | UniswapV2Pair],
        path: list[Token],
    ):
        self.pools = [as_liquidity_quote(p) for p in pools]
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

    def quote_hops(self, amount_in: int) -> list[QuoteResult]:
        """Quote each hop in order; output of hop *i* is input to hop *i+1*."""
        if amount_in <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in}")
        out: list[QuoteResult] = []
        cur = amount_in
        for i, pool in enumerate(self.pools):
            t = self.path[i]
            qr = pool.quote_exact_input(t, cur)
            out.append(qr)
            cur = qr.amount_out
        return out

    def get_output(self, amount_in: int) -> int:
        """Simulate full route, return final output."""
        hops = self.quote_hops(amount_in)
        return hops[-1].amount_out if hops else amount_in

    def get_intermediate_amounts(self, amount_in: int) -> list[int]:
        """Return amount at each step: [input, after_hop1, after_hop2, ...]"""
        amounts: list[int] = [amount_in]
        for qr in self.quote_hops(amount_in):
            amounts.append(qr.amount_out)
        return amounts

    def estimate_gas(self, amount_in: int | None = None) -> int:
        """
        Gas units for the full route.

        With ``amount_in > 0``, uses the sum of per-hop ``QuoteResult.gas_estimate`` (Quoter on
        V3; V2 adapter uses a flat per-hop heuristic).

        With ``amount_in`` omitted or non-positive, uses the legacy router-style heuristic
        (~150k + ~100k per extra hop) when the trade size is unknown or RPC should be avoided.
        """
        n = self.num_hops
        if n <= 0:
            return 0
        if amount_in is None or amount_in <= 0:
            return _BASE_SWAP_GAS + _GAS_PER_EXTRA_HOP * (n - 1)
        return sum(q.gas_estimate for q in self.quote_hops(amount_in))
