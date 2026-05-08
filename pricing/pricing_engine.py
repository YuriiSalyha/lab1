"""Orchestrates routing, fork simulation, and mempool monitoring.

Pool loading and fork swap execution here are **Uniswap V2 router** only
(:func:`~pricing.fork_swap_executor.execute_swap_exact_tokens_for_tokens_on_fork`).
:class:`~pricing.route_finder.RouteFinder` may quote mixed V2/V3 routes elsewhere, but this
module does not simulate V3 swaps on a fork until a V3 execution path exists.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from chain.client import ChainClient
from core.types import Address, Token
from core.wallet import WalletManager
from pricing.fork_simulator import ForkSimulator, SimulationResult
from pricing.fork_swap_executor import (
    ForkSwapExecutionResult,
    execute_swap_exact_tokens_for_tokens_on_fork,
)
from pricing.liquidity_pool import (
    LiquidityPoolQuote,
    UniswapV2PoolAdapter,
)
from pricing.mempool_monitor import MempoolMonitor
from pricing.parsed_swap import ParsedSwap
from pricing.route import Route
from pricing.route_finder import RouteFinder
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter

logger = logging.getLogger(__name__)

# Arbitrum One Uniswap V2 router (same ABI as Ethereum mainnet).
DEFAULT_UNISWAP_V2_ROUTER = Address("0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24")


def _resolve_swap_router(swap_router: Optional[Address]) -> Address:
    if swap_router is not None:
        return swap_router
    raw = os.getenv("UNISWAP_V2_ROUTER", "").strip()
    if raw:
        return Address.from_string(raw)
    return DEFAULT_UNISWAP_V2_ROUTER


# uint256 max — always satisfies router deadline checks on a fork.
_DEFAULT_QUOTE_DEADLINE = 2**256 - 1
_MAX_MEMPOOL_EVENTS = 256
# Relative tolerance for gross_output vs fork simulation (0.001 = 0.1%).
_IS_VALID_REL_TOLERANCE = 0.001


class QuoteError(Exception):
    """Raised when a price quote cannot be produced (e.g. simulation fails, no route)."""


@dataclass
class Quote:
    """Single swap quote: off-chain route math plus fork simulation.

    ``gross_output`` matches on-chain swap output (before gas, in *token_out* units).
    ``net_output`` subtracts estimated gas cost priced in *token_out* (see
    :meth:`RouteFinder.find_best_route`). ``is_valid`` compares gross to simulation.
    """

    route: Route
    amount_in: int
    gross_output: int
    net_output: int
    simulated_output: int
    gas_estimate: int
    timestamp: float

    def is_valid(self) -> bool:
        if self.gross_output <= 0:
            return False
        rel = abs(self.gross_output - self.simulated_output) / self.gross_output
        return rel < _IS_VALID_REL_TOLERANCE


class PricingEngine:
    """
    Integrates :class:`ChainClient`, :class:`RouteFinder`, :class:`ForkSimulator`,
    and :class:`MempoolMonitor`.

    Callers must run ``await engine.monitor.start()`` themselves if they want live
    mempool events. ``quote_sender`` must have balance and router allowance on the
    fork used by ``fork_url`` when calling :meth:`get_quote`.
    """

    def __init__(
        self,
        chain_client: ChainClient,
        fork_url: str,
        ws_url: str,
        quote_sender: Address,
        *,
        swap_router: Optional[Address] = None,
    ) -> None:
        self.client = chain_client
        self.simulator = ForkSimulator(fork_url)
        self.monitor = MempoolMonitor(ws_url, self._on_mempool_swap)
        self.pools: dict[Address, UniswapV2Pair] = {}
        self.v3_pools: dict[Address, UniswapV3PoolQuoter] = {}
        self.route_finder: Optional[RouteFinder] = None
        self.swap_router = _resolve_swap_router(swap_router)
        self.quote_sender = quote_sender
        self._mempool_affects: deque[tuple[ParsedSwap, list[Address]]] = deque(
            maxlen=_MAX_MEMPOOL_EVENTS
        )

    def _all_quotes(self) -> list[LiquidityPoolQuote]:
        """Unified ``LiquidityPoolQuote`` list (V2 wrapped via adapter, V3 used directly)."""
        out: list[LiquidityPoolQuote] = [UniswapV2PoolAdapter(p) for p in self.pools.values()]
        out.extend(self.v3_pools.values())
        return out

    def _rebuild_route_finder(self) -> None:
        self.route_finder = RouteFinder(self._all_quotes())

    def load_pools(self, pool_addresses: list[Address]) -> None:
        """Fetch each V2 pool from chain and rebuild the route graph."""
        for addr in pool_addresses:
            self.pools[addr] = UniswapV2Pair.from_chain(addr, self.client)
        self._rebuild_route_finder()

    def refresh_pool(self, address: Address) -> None:
        """Re-fetch reserves for one V2 pool and rebuild :class:`RouteFinder`."""
        if address not in self.pools:
            raise QuoteError(f"Pool not loaded: {address.checksum}")
        self.pools[address] = UniswapV2Pair.from_chain(address, self.client)
        self._rebuild_route_finder()

    def load_pools_v3(
        self,
        pool_addresses: list[Address],
        *,
        quoter_address: str | None = None,
    ) -> None:
        """Fetch each V3 pool's metadata (token0/token1/fee) and rebuild the route graph.

        V3 quoting is stateless via ``QuoterV2`` (``eth_call``), so no per-tick reserve
        refresh is required — the metadata read here happens once at load time.
        """
        for addr in pool_addresses:
            self.v3_pools[addr] = UniswapV3PoolQuoter.from_chain(
                addr,
                self.client,
                quoter_address=quoter_address,
            )
        self._rebuild_route_finder()

    def refresh_pool_v3(self, address: Address) -> None:
        """Re-read V3 pool meta (rare; ``fee`` and tokens are immutable) and rebuild router.

        Useful when a previously-failed metadata read is being retried; safe no-op
        for already-loaded pools at the protocol level.
        """
        if address not in self.v3_pools:
            raise QuoteError(f"V3 pool not loaded: {address.checksum}")
        self.v3_pools[address] = UniswapV3PoolQuoter.from_chain(address, self.client)
        self._rebuild_route_finder()

    def affected_pool_addresses(self, swap: ParsedSwap) -> list[Address]:
        """Pools whose token pair intersects the swap's token_in / token_out."""
        if swap.token_in is None or swap.token_out is None:
            return []
        ends = {swap.token_in.lower, swap.token_out.lower}
        return [
            addr
            for addr, pool in self.pools.items()
            if pool.token0.address.lower in ends or pool.token1.address.lower in ends
        ]

    def _on_mempool_swap(self, swap: ParsedSwap) -> None:
        addrs = self.affected_pool_addresses(swap)
        if not addrs:
            return
        self._mempool_affects.append((swap, addrs))
        logger.debug(
            "mempool swap %s affects pools %s",
            swap.tx_hash[:18] if swap.tx_hash else "?",
            [a.checksum for a in addrs],
        )

    def get_quote(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
        *,
        max_hops: int = 3,
        eth_price_in_output: int | None = None,
        deadline: int | None = None,
    ) -> Quote:
        """
        Best route by net output, verified with :meth:`ForkSimulator.simulate_route`.

        Raises:
            QuoteError: No pools / route, or fork simulation reverted.
        """
        if self.route_finder is None or not self.pools:
            raise QuoteError("No pools loaded; call load_pools first")

        route, net_output = self.route_finder.find_best_route(
            token_in,
            token_out,
            amount_in,
            gas_price_gwei,
            max_hops=max_hops,
            eth_price_in_output=eth_price_in_output,
        )
        if route is None:
            raise QuoteError(f"No route from {token_in!r} to {token_out!r}")

        gross = route.get_output(amount_in)
        dl = _DEFAULT_QUOTE_DEADLINE if deadline is None else int(deadline)

        sim: SimulationResult = self.simulator.simulate_route(
            self.swap_router,
            route,
            amount_in,
            self.quote_sender,
            dl,
            amount_out_min=0,
        )
        if not sim.success:
            raise QuoteError(f"Simulation failed: {sim.error}")

        return Quote(
            route=route,
            amount_in=amount_in,
            gross_output=gross,
            net_output=net_output,
            simulated_output=sim.amount_out,
            gas_estimate=sim.gas_used,
            timestamp=time.time(),
        )

    @staticmethod
    def _resolve_pool_base_quote(
        pool: UniswapV2Pair | UniswapV3PoolQuoter,
        base_token: Token,
    ) -> tuple[Token, Token]:
        """Order ``(pool_base, pool_quote)`` against the caller's base symbol/address."""
        from strategy.dex_token_resolver import symbol_match

        pool_base = pool.token0 if pool.token0 == base_token else pool.token1
        if pool_base != base_token:
            if symbol_match(base_token.symbol, pool.token0):
                pool_base = pool.token0
            elif symbol_match(base_token.symbol, pool.token1):
                pool_base = pool.token1
            else:
                raise QuoteError(
                    f"Base token {base_token.symbol} not on pool {pool.address.checksum}",
                )
        pool_quote = pool.token1 if pool_base == pool.token0 else pool.token0
        return pool_base, pool_quote

    def _resolve_v2_pool_assets(
        self,
        base_token: Token,
        quote_token: Token,
    ) -> tuple[UniswapV2Pair, Token, Token]:
        from strategy.dex_token_resolver import find_pool_for_pair

        pool = find_pool_for_pair(self.pools, base_token.symbol, quote_token.symbol)
        pool_base, pool_quote = self._resolve_pool_base_quote(pool, base_token)
        return pool, pool_base, pool_quote

    def _resolve_pool_for_pair(
        self,
        base_token: Token,
        quote_token: Token,
    ) -> tuple[UniswapV2Pair | UniswapV3PoolQuoter, Token, Token, str]:
        """Return ``(pool, pool_base, pool_quote, kind)`` where ``kind in {"v2","v3"}``.

        Prefers V2 when it matches (preserves V2-only behavior); falls back to V3 only
        when no V2 pool exists for the pair.
        """
        from strategy.dex_token_resolver import find_pool_for_pair, find_v3_pools_for_pair

        if self.pools:
            try:
                pool = find_pool_for_pair(self.pools, base_token.symbol, quote_token.symbol)
                pb, pq = self._resolve_pool_base_quote(pool, base_token)
                return pool, pb, pq, "v2"
            except ValueError:
                if not self.v3_pools:
                    raise
        v3_matches = find_v3_pools_for_pair(self.v3_pools, base_token.symbol, quote_token.symbol)
        if not v3_matches:
            raise QuoteError(
                f"No Uniswap V2 or V3 pool loaded for {base_token.symbol}/{quote_token.symbol}.",
            )
        v3_pool = v3_matches[0]
        pb, pq = self._resolve_pool_base_quote(v3_pool, base_token)
        return v3_pool, pb, pq, "v3"

    @staticmethod
    def _spot_quote_per_base_human(
        pool: UniswapV2Pair,
        pool_base: Token,
        pool_quote: Token,
    ) -> Decimal:
        """Reserve ratio quote/base in human units (no swap fee, no trade size)."""
        if pool_base == pool.token0:
            r_base, r_quote = pool.reserve0, pool.reserve1
        else:
            r_base, r_quote = pool.reserve1, pool.reserve0
        base_h = Decimal(r_base) / (Decimal(10) ** pool_base.decimals)
        quote_h = Decimal(r_quote) / (Decimal(10) ** pool_quote.decimals)
        if base_h <= 0:
            return Decimal("0")
        return quote_h / base_h

    def get_spot_quote_per_base_human(self, base_token: Token, quote_token: Token) -> Decimal:
        """Reserve ratio quote/base (human units), ignoring trade size and swap fee."""
        if not self.pools:
            raise QuoteError("No pools loaded; call load_pools first")
        pool, pool_base, pool_quote = self._resolve_v2_pool_assets(base_token, quote_token)
        return self._spot_quote_per_base_human(pool, pool_base, pool_quote)

    @staticmethod
    def _v3_quote_atoms(
        pool: UniswapV3PoolQuoter,
        token_in: Token,
        amount_in_atoms: int,
    ) -> int:
        """Wrap ``QuoterV2.quoteExactInputSingle`` returning the output amount.

        Returns ``0`` if the quoter reverts or returns no liquidity (e.g. tier
        without enough depth at this size); callers treat this as "skip this
        candidate" so a worse-but-valid pool can still win selection.
        """
        try:
            res = pool.quote_exact_input(token_in, amount_in_atoms)
        except Exception:
            return 0
        return int(res.amount_out) if res.amount_out > 0 else 0

    def _v3_pair_prices(
        self,
        pool: UniswapV3PoolQuoter,
        pool_base: Token,
        pool_quote: Token,
        base_size: Decimal,
        base_atoms: int,
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        """``(dex_buy, dex_sell, spot)`` for one V3 pool, or ``None`` if both directions fail.

        Spot is approximated as the marginal quote at a tiny probe size
        (``min(1 base atom, 1e-6 of base_size)``) to avoid relying on
        ``sqrtPriceX96`` decoding on every signal tick.
        """
        q_dec = pool_quote.decimals
        sell_atoms = self._v3_quote_atoms(pool, pool_base, base_atoms)
        # Buy direction: V3 has no exact-output single-call quoter wired up here, so we
        # invert the exact-input quote in the *opposite* direction (quote -> base) by
        # bisection-style scaling: estimate quote needed to obtain ``base_atoms`` base.
        # Using a single-shot inverse via "reverse" quote: ``quote_atoms`` such that
        # quoting that amount of quote_in gives back >= base_atoms.
        # Cheaper proxy: quote (base_atoms) in opposite direction at ``sell_atoms``-implied
        # price; sufficient for signal generation. Falls back to executor for exact pricing.
        if sell_atoms <= 0:
            buy_atoms = 0
        else:
            # Inverse quote: how much quote_in gives back base_atoms_back ~= base_atoms.
            # Using the reverse exact-in quote: feed ``sell_atoms`` quote and check the
            # resulting base; then scale price by base/base_back ratio.
            base_back = self._v3_quote_atoms(pool, pool_quote, sell_atoms)
            if base_back <= 0:
                buy_atoms = 0
            else:
                # sell_atoms quote_in -> base_back base_out. Effective buy price for
                # ``base_atoms`` base ≈ sell_atoms * (base_atoms / base_back). Round up.
                buy_atoms = (sell_atoms * base_atoms + base_back - 1) // base_back
        if sell_atoms <= 0 and buy_atoms <= 0:
            return None
        sell_human = (
            Decimal(sell_atoms) / (Decimal(10) ** q_dec) if sell_atoms > 0 else Decimal("0")
        )
        buy_human = Decimal(buy_atoms) / (Decimal(10) ** q_dec) if buy_atoms > 0 else Decimal("0")
        dex_sell = sell_human / base_size if sell_human > 0 else Decimal("0")
        dex_buy = buy_human / base_size if buy_human > 0 else Decimal("0")
        # Spot proxy: marginal quote at 1 base atom (or 1e-6 of base_size, whichever bigger).
        probe_atoms = max(1, base_atoms // 1_000_000)
        probe_out = self._v3_quote_atoms(pool, pool_base, probe_atoms)
        if probe_out > 0:
            probe_base_h = Decimal(probe_atoms) / (Decimal(10) ** pool_base.decimals)
            probe_quote_h = Decimal(probe_out) / (Decimal(10) ** q_dec)
            spot = probe_quote_h / probe_base_h if probe_base_h > 0 else Decimal("0")
        else:
            spot = (
                (dex_buy + dex_sell) / Decimal(2) if dex_buy > 0 and dex_sell > 0 else Decimal("0")
            )
        return dex_buy, dex_sell, spot

    def _v2_pair_prices(
        self,
        pool: UniswapV2Pair,
        pool_base: Token,
        pool_quote: Token,
        base_size: Decimal,
        base_atoms: int,
    ) -> tuple[Decimal, Decimal, Decimal]:
        q_dec = pool_quote.decimals
        quote_in_atoms = pool.get_amount_in(base_atoms, pool_base)
        quote_out_atoms = pool.get_amount_out(base_atoms, pool_base)
        quote_in_human = Decimal(quote_in_atoms) / (Decimal(10) ** q_dec)
        quote_out_human = Decimal(quote_out_atoms) / (Decimal(10) ** q_dec)
        dex_buy = quote_in_human / base_size
        dex_sell = quote_out_human / base_size
        spot = self._spot_quote_per_base_human(pool, pool_base, pool_quote)
        return dex_buy, dex_sell, spot

    def get_pair_prices_math(
        self,
        base_token: Token,
        quote_token: Token,
        base_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Math-only ``(dex_buy_price, dex_sell_price, spot_quote_per_base)`` for ``BASE/QUOTE``.

        Picks the best DEX pool across loaded V2 pairs and V3 fee tiers:
        higher ``dex_sell`` and lower ``dex_buy`` win independently. When
        only V2 is loaded the result is byte-identical to the legacy
        constant-product math (no V3 RPC traffic).

        - V2 candidates use integer constant-product math against
          currently-loaded reserves (cheap; reserves refreshed by ``ArbBot``
          on ``ARB_POOL_REFRESH_SECONDS`` cadence).
        - V3 candidates issue ``eth_call`` against ``QuoterV2`` for both
          directions; failures (no liquidity, revert) silently skip the
          candidate so a working V2 pool still wins.

        Raises:
            QuoteError: No matching pool of any kind, or invalid size.
        """
        dex_buy, dex_sell, spot, _pool, _kind = self.get_pair_prices_math_with_pool(
            base_token, quote_token, base_size
        )
        return dex_buy, dex_sell, spot

    def get_pair_prices_math_with_pool(
        self,
        base_token: Token,
        quote_token: Token,
        base_size: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, UniswapV2Pair | UniswapV3PoolQuoter, str]:
        """Same as :meth:`get_pair_prices_math` but also returns ``(pool, kind)``.

        ``kind`` is ``"v2"`` or ``"v3"``. Used by the executor to dispatch the
        right swap calldata without re-resolving.
        """
        from strategy.dex_token_resolver import find_candidates_for_pair

        if base_size <= 0:
            raise QuoteError(f"base_size must be positive, got {base_size}")
        if not self.pools and not self.v3_pools:
            raise QuoteError("No pools loaded; call load_pools first")

        candidates = find_candidates_for_pair(
            self.pools, self.v3_pools, base_token.symbol, quote_token.symbol
        )
        if not candidates:
            raise QuoteError(
                f"No Uniswap V2 or V3 pool loaded for {base_token.symbol}/{quote_token.symbol}.",
            )

        # Compute prices for each candidate independently; tracking best buy / best sell.
        best_buy: Decimal | None = None
        best_sell: Decimal | None = None
        best_spot: Decimal | None = None
        best_pool: UniswapV2Pair | UniswapV3PoolQuoter | None = None
        best_kind: str = ""
        # When best_buy and best_sell come from different pools, prefer the pool that
        # wins on dex_sell (the leg the bot is more likely to execute on the DEX side).
        for pool in candidates:
            try:
                pool_base, pool_quote = self._resolve_pool_base_quote(pool, base_token)
            except QuoteError:
                continue
            base_atoms = int(base_size * (Decimal(10) ** pool_base.decimals))
            if base_atoms <= 0:
                continue
            if isinstance(pool, UniswapV2Pair):
                buy, sell, spot = self._v2_pair_prices(
                    pool, pool_base, pool_quote, base_size, base_atoms
                )
                kind = "v2"
            else:
                got = self._v3_pair_prices(pool, pool_base, pool_quote, base_size, base_atoms)
                if got is None:
                    continue
                buy, sell, spot = got
                kind = "v3"
            if sell <= 0 and buy <= 0:
                continue
            if best_pool is None or (sell > 0 and (best_sell is None or sell > best_sell)):
                best_pool = pool
                best_kind = kind
                best_spot = spot
            if buy > 0 and (best_buy is None or buy < best_buy):
                best_buy = buy
            if sell > 0 and (best_sell is None or sell > best_sell):
                best_sell = sell

        if best_pool is None or best_spot is None:
            raise QuoteError(
                f"No usable quote for {base_token.symbol}/{quote_token.symbol} at size {base_size}",
            )
        return (
            best_buy or Decimal("0"),
            best_sell or Decimal("0"),
            best_spot,
            best_pool,
            best_kind,
        )

    def execute_route_on_fork(
        self,
        fork_client: ChainClient,
        wallet: WalletManager,
        route: Route,
        amount_in: int,
        amount_out_min: int,
        *,
        deadline: int | None = None,
        run_preflight: bool = True,
        recipient: Address | None = None,
    ) -> ForkSwapExecutionResult:
        """Broadcast swap on fork RPC (see :func:`execute_swap_exact_tokens_for_tokens_on_fork`)."""
        dl = _DEFAULT_QUOTE_DEADLINE if deadline is None else int(deadline)
        return execute_swap_exact_tokens_for_tokens_on_fork(
            fork_client,
            wallet,
            self.swap_router,
            route,
            amount_in,
            amount_out_min,
            dl,
            run_preflight=run_preflight,
            recipient=recipient,
        )
