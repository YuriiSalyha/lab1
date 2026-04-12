"""Abstract liquidity pool quoting (V2, V3, …) for routing and arbitrage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair

# V2 per-hop gas hint for QuoteResult; Route.estimate_gas keeps the legacy multi-hop heuristic.
_V2_QUOTE_GAS_UNITS = 150_000


@dataclass(frozen=True, slots=True)
class QuoteResult:
    """Output amount and gas units for one exact-in swap hop."""

    amount_out: int
    gas_estimate: int


@runtime_checkable
class LiquidityPoolQuote(Protocol):
    """Minimal surface for graph routing and simulation."""

    @property
    def address(self) -> Address: ...

    @property
    def token0(self) -> Token: ...

    @property
    def token1(self) -> Token: ...

    def pool_id(self) -> str:
        """Stable key for deduplication (lowercase address or address:fee for V3)."""
        ...

    def quote_exact_input(self, token_in: Token, amount_in: int) -> QuoteResult: ...


@dataclass
class UniswapV2PoolAdapter:
    """Wraps :class:`UniswapV2Pair` as :class:`LiquidityPoolQuote` without duplicating math."""

    _pair: UniswapV2Pair

    @property
    def pair(self) -> UniswapV2Pair:
        return self._pair

    @property
    def address(self) -> Address:
        return self._pair.address

    @property
    def token0(self) -> Token:
        return self._pair.token0

    @property
    def token1(self) -> Token:
        return self._pair.token1

    def pool_id(self) -> str:
        return self._pair.address.lower

    def quote_exact_input(self, token_in: Token, amount_in: int) -> QuoteResult:
        out = self._pair.get_amount_out(amount_in, token_in)
        return QuoteResult(amount_out=out, gas_estimate=_V2_QUOTE_GAS_UNITS)


def as_liquidity_quote(pool: LiquidityPoolQuote | UniswapV2Pair) -> LiquidityPoolQuote:
    """Normalize to :class:`LiquidityPoolQuote` (wrap raw V2 pairs)."""
    if isinstance(pool, UniswapV2PoolAdapter):
        return pool
    if isinstance(pool, UniswapV2Pair):
        return UniswapV2PoolAdapter(pool)
    return pool


def v2_pools_for_gas_pricing(pools: list[LiquidityPoolQuote]) -> list[UniswapV2Pair]:
    """Pairs whose reserves can price ETH gas in ERC-20 output (V2 only)."""
    out: list[UniswapV2Pair] = []
    for p in pools:
        if isinstance(p, UniswapV2PoolAdapter):
            out.append(p.pair)
        elif isinstance(p, UniswapV2Pair):
            out.append(p)
    return out
