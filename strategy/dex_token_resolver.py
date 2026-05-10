"""Map CEX-style ``BASE/QUOTE`` symbols to on-chain :class:`~core.types.Token` pairs.

Treats **ETH/WETH**, **BTC/WBTC**, and **USDT/USDC** (incl. ``*.e``-style and
``USD₮0`` / ``USDT0`` Arbitrum-bridged variants) as compatible so a CEX
``BASE/USDT`` pair can resolve against a DEX pool quoted in USDC or
``USD₮0`` (the Arbitrum native USDT token symbol).

Used by :class:`~strategy.generator.SignalGenerator` when a
:class:`~pricing.pricing_engine.PricingEngine` has loaded Uniswap V2 pools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter

if TYPE_CHECKING:
    from pricing.pricing_engine import PricingEngine

# Union of pool types that can be resolved for a CEX pair.
PoolCandidate = UniswapV2Pair | UniswapV3PoolQuoter


def _normalize_symbol(sym: str) -> str:
    """Upper-case + map known Tether glyphs/aliases to plain ``USDT``.

    Arbitrum's bridged Tether registers its symbol as ``USD₮0`` (using the
    Tether glyph ``U+20AE``). Some block explorers / RPC metadata also surface
    it as ``USDT0`` (ASCII). Both are pegged 1:1 USDT and should match a CEX
    ``USDT`` quote symbol for routing purposes.
    """
    s = sym.strip().upper()
    s = s.replace("₮", "T")  # Tether glyph (U+20AE) → plain T
    if s in ("USDT0", "USDT.E", "USDTE"):
        return "USDT"
    return s


def _usd_stable_core(sym: str) -> str | None:
    """USDT/USDC (and ``*.e`` bridged + ``USD₮0`` variants) for pegged-dollar matching."""
    base = _normalize_symbol(sym).split(".", 1)[0]
    if base in ("USDT", "USDC"):
        return base
    return None


def symbol_match(pair_sym: str, token: Token) -> bool:
    """True if CEX symbol ``pair_sym`` refers to the same asset as ``token``."""
    s = _normalize_symbol(pair_sym)
    t = _normalize_symbol(token.symbol)
    if s == t:
        return True
    if {s, t} <= {"ETH", "WETH"}:
        return True
    if {s, t} <= {"BTC", "WBTC"}:
        return True
    if _usd_stable_core(s) is not None and _usd_stable_core(t) is not None:
        return True
    return False


def _pool_matches_pair(
    pool: PoolCandidate,
    base_sym: str,
    quote_sym: str,
) -> bool:
    ok_b0 = symbol_match(base_sym, pool.token0) and symbol_match(quote_sym, pool.token1)
    ok_b1 = symbol_match(base_sym, pool.token1) and symbol_match(quote_sym, pool.token0)
    return ok_b0 or ok_b1


def find_pool_for_pair(
    pools: dict[Address, UniswapV2Pair],
    base_sym: str,
    quote_sym: str,
) -> UniswapV2Pair:
    """Return the unique V2 pool whose tokens match ``base_sym`` / ``quote_sym``."""
    for pool in pools.values():
        if _pool_matches_pair(pool, base_sym, quote_sym):
            return pool
    loaded = (
        ", ".join(f"{p.token0.symbol}/{p.token1.symbol}" for p in pools.values())
        if pools
        else "(none)"
    )
    raise ValueError(
        f"No Uniswap V2 pool loaded for {base_sym}/{quote_sym}. "
        f"Loaded V2 pairs: {loaded}. "
        "Set V2_POOLS, V2_POOL, or ARB_V2_POOLS to include a pool for this pair.",
    )


def find_v3_pools_for_pair(
    pools: dict[Address, UniswapV3PoolQuoter],
    base_sym: str,
    quote_sym: str,
) -> list[UniswapV3PoolQuoter]:
    """Return all V3 pools (every fee tier) whose tokens match ``base_sym`` / ``quote_sym``."""
    return [p for p in pools.values() if _pool_matches_pair(p, base_sym, quote_sym)]


def find_candidates_for_pair(
    v2_pools: dict[Address, UniswapV2Pair],
    v3_pools: dict[Address, UniswapV3PoolQuoter],
    base_sym: str,
    quote_sym: str,
) -> list[PoolCandidate]:
    """Return every loaded V2 + V3 pool that matches the pair (any order, any fee tier)."""
    out: list[PoolCandidate] = [
        p for p in v2_pools.values() if _pool_matches_pair(p, base_sym, quote_sym)
    ]
    out.extend(find_v3_pools_for_pair(v3_pools, base_sym, quote_sym))
    return out


def base_quote_tokens(pool: PoolCandidate, base_sym: str, quote_sym: str) -> tuple[Token, Token]:
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
    """Build a ``pair -> (base_token, quote_token)`` callable for :class:`SignalGenerator`.

    Resolves against any loaded V2 *or* V3 pool. V2 takes precedence so that
    V2-only deployments (no ``V3_POOLS`` / ``V3_AUTO_DISCOVER`` set) are
    behaviourally identical to before V3 support was added.
    """

    def resolve(pair: str) -> tuple[Token, Token]:
        v3_pools = getattr(engine, "v3_pools", {})
        if not engine.pools and not v3_pools:
            raise ValueError("PricingEngine has no pools loaded")
        parts = pair.strip().upper().split("/")
        if len(parts) != 2:
            raise ValueError(f"pair must be BASE/QUOTE, got {pair!r}")
        base_sym, quote_sym = parts[0], parts[1]
        if engine.pools:
            try:
                pool = find_pool_for_pair(engine.pools, base_sym, quote_sym)
                return base_quote_tokens(pool, base_sym, quote_sym)
            except ValueError:
                if not v3_pools:
                    raise
        v3_matches = find_v3_pools_for_pair(v3_pools, base_sym, quote_sym)
        if not v3_matches:
            raise ValueError(
                f"No Uniswap V2 or V3 pool loaded for {base_sym}/{quote_sym}.",
            )
        return base_quote_tokens(v3_matches[0], base_sym, quote_sym)

    return resolve
