"""Map CEX-style ``BASE/QUOTE`` symbols to on-chain :class:`~core.types.Token` pairs.

Used by :class:`~strategy.generator.SignalGenerator` when a
:class:`~pricing.pricing_engine.PricingEngine` has loaded Uniswap V2 pools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair

if TYPE_CHECKING:
    from pricing.pricing_engine import PricingEngine


def symbol_match(pair_sym: str, token: Token) -> bool:
    """True if CEX symbol ``pair_sym`` refers to the same asset as ``token``."""
    s = pair_sym.upper()
    t = token.symbol.upper()
    if s == t:
        return True
    if {s, t} <= {"ETH", "WETH"}:
        return True
    if {s, t} <= {"BTC", "WBTC"}:
        return True
    return False


def find_pool_for_pair(
    pools: dict[Address, UniswapV2Pair],
    base_sym: str,
    quote_sym: str,
) -> UniswapV2Pair:
    """Return the unique V2 pool whose tokens match ``base_sym`` / ``quote_sym``."""
    for pool in pools.values():
        ok_b0 = symbol_match(base_sym, pool.token0) and symbol_match(quote_sym, pool.token1)
        ok_b1 = symbol_match(base_sym, pool.token1) and symbol_match(quote_sym, pool.token0)
        if ok_b0 or ok_b1:
            return pool
    raise ValueError(
        f"No Uniswap V2 pool loaded for {base_sym}/{quote_sym}. "
        "Extend ARB_V2_POOLS or load a pool that lists both tokens.",
    )


def base_quote_tokens(pool: UniswapV2Pair, base_sym: str, quote_sym: str) -> tuple[Token, Token]:
    """Order pool tokens as (base, quote) following ``base_sym`` / ``quote_sym``."""
    base_t: Token | None = None
    quote_t: Token | None = None
    for t in (pool.token0, pool.token1):
        if symbol_match(base_sym, t):
            base_t = t
            break
    if base_t is None:
        raise ValueError(f"Base token {base_sym} not found on pool {pool.address.checksum}")
    for t in (pool.token0, pool.token1):
        if symbol_match(quote_sym, t):
            quote_t = t
            break
    if quote_t is None:
        raise ValueError(f"Quote token {quote_sym} not found on pool {pool.address.checksum}")
    return base_t, quote_t


def token_resolver_from_pricing_engine(
    engine: PricingEngine,
) -> Callable[[str], tuple[Token, Token]]:
    """Build a ``pair -> (base_token, quote_token)`` callable for :class:`SignalGenerator`."""

    def resolve(pair: str) -> tuple[Token, Token]:
        if not engine.pools:
            raise ValueError("PricingEngine has no pools loaded")
        parts = pair.strip().upper().split("/")
        if len(parts) != 2:
            raise ValueError(f"pair must be BASE/QUOTE, got {pair!r}")
        base_sym, quote_sym = parts[0], parts[1]
        pool = find_pool_for_pair(engine.pools, base_sym, quote_sym)
        return base_quote_tokens(pool, base_sym, quote_sym)

    return resolve
