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
from pricing.mempool_monitor import MempoolMonitor
from pricing.parsed_swap import ParsedSwap
from pricing.route import Route
from pricing.route_finder import RouteFinder
from pricing.uniswap_v2_pair import UniswapV2Pair

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
        self.route_finder: Optional[RouteFinder] = None
        self.swap_router = _resolve_swap_router(swap_router)
        self.quote_sender = quote_sender
        self._mempool_affects: deque[tuple[ParsedSwap, list[Address]]] = deque(
            maxlen=_MAX_MEMPOOL_EVENTS
        )

    def load_pools(self, pool_addresses: list[Address]) -> None:
        """Fetch each pool from chain and rebuild the route graph."""
        for addr in pool_addresses:
            self.pools[addr] = UniswapV2Pair.from_chain(addr, self.client)
        self.route_finder = RouteFinder(list(self.pools.values()))

    def refresh_pool(self, address: Address) -> None:
        """Re-fetch reserves for one pool and rebuild :class:`RouteFinder`."""
        if address not in self.pools:
            raise QuoteError(f"Pool not loaded: {address.checksum}")
        self.pools[address] = UniswapV2Pair.from_chain(address, self.client)
        self.route_finder = RouteFinder(list(self.pools.values()))

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

    def get_pair_prices_math(
        self,
        base_token: Token,
        quote_token: Token,
        base_size: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Math-only ``(dex_buy_price, dex_sell_price)`` for ``BASE/QUOTE``.

        Both prices are quote-per-base, in human units:

        - ``dex_buy``  = how much QUOTE we would pay per unit BASE if buying
          ``base_size`` of BASE through the loaded V2 pool (``getAmountIn``).
        - ``dex_sell`` = how much QUOTE we would receive per unit BASE if
          selling ``base_size`` of BASE (``getAmountOut``).

        Pure constant-product math against currently-loaded reserves; no fork
        ``eth_call``, no ``quote_sender`` requirement, no router approval. Use
        :meth:`get_quote` instead when you need the result verified against
        a fork (e.g. before signing a real transaction).

        Reserves are not refreshed here — call :meth:`refresh_pool` periodically
        so the price tracks live LP activity (``ArbBot`` does this on a
        ``ARB_POOL_REFRESH_SECONDS`` cadence).

        Raises:
            QuoteError: No direct V2 pool loaded for the pair, or invalid size.
        """
        if base_size <= 0:
            raise QuoteError(f"base_size must be positive, got {base_size}")
        if not self.pools:
            raise QuoteError("No pools loaded; call load_pools first")

        # Local import avoids the strategy -> pricing -> strategy cycle that
        # would otherwise show up at module-load time.
        from strategy.dex_token_resolver import find_pool_for_pair

        pool = find_pool_for_pair(self.pools, base_token.symbol, quote_token.symbol)
        # Resolve which side of the pool actually carries each token; symbol
        # match (BASE vs WBASE) does not guarantee object identity.
        pool_base = pool.token0 if pool.token0 == base_token else pool.token1
        if pool_base != base_token:
            # Fall back to symbol-based identification when the resolver passed
            # a Token instance built from CEX-style metadata that does not
            # equal-compare with the on-chain Token loaded into the pool.
            from strategy.dex_token_resolver import symbol_match

            if symbol_match(base_token.symbol, pool.token0):
                pool_base = pool.token0
            elif symbol_match(base_token.symbol, pool.token1):
                pool_base = pool.token1
            else:
                raise QuoteError(
                    f"Base token {base_token.symbol} not on pool {pool.address.checksum}",
                )

        base_atoms = int(base_size * (Decimal(10) ** pool_base.decimals))
        if base_atoms <= 0:
            raise QuoteError(
                f"base_size {base_size} rounded down to zero atoms (decimals={pool_base.decimals})",
            )

        quote_in_atoms = pool.get_amount_in(base_atoms, pool_base)
        quote_out_atoms = pool.get_amount_out(base_atoms, pool_base)
        quote_decimals = quote_token.decimals
        quote_in_human = Decimal(quote_in_atoms) / (Decimal(10) ** quote_decimals)
        quote_out_human = Decimal(quote_out_atoms) / (Decimal(10) ** quote_decimals)
        dex_buy = quote_in_human / base_size
        dex_sell = quote_out_human / base_size
        return dex_buy, dex_sell

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
