"""Execution engine: coordinates CEX and DEX legs for one arbitrage signal.

State machine:

.. code-block:: text

    IDLE -> VALIDATING -> LEG1_PENDING -> LEG1_FILLED
                                       \\-> FAILED
                       \\-> LEG2_PENDING -> DONE
                                         \\-> UNWINDING -> FAILED

All monetary math uses :class:`~decimal.Decimal`. Time durations (timeouts,
latencies) remain ``float`` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Callable, Optional

from executor.circuit_breaker import CircuitBreaker
from executor.replay_protection import ReplayProtection
from strategy.fees import FeeStructure
from strategy.signal import Direction, Signal, to_decimal

logger = logging.getLogger(__name__)

# --- Executor defaults (all user-tunable via ExecutorConfig) -----------------
DEFAULT_LEG1_TIMEOUT_S = 5.0
DEFAULT_LEG2_TIMEOUT_S = 60.0
DEFAULT_MIN_FILL_RATIO = Decimal("0.8")
# CEX limit price is padded vs the quoted price so IOC crosses the book.
CEX_SLIPPAGE_PAD = Decimal("1.001")

# Simulation-mode fill adjustments (intentionally small so PnL stays close to expected).
SIMULATION_CEX_PRICE_ADJUST = Decimal("1.0001")
SIMULATION_DEX_PRICE_ADJUST = Decimal("0.9998")
SIMULATION_CEX_LATENCY_S = 0.1
SIMULATION_DEX_LATENCY_S = 0.5
SIMULATION_UNWIND_LATENCY_S = 0.1

# Venue tags used in context + recorded outputs.
VENUE_CEX = "cex"
VENUE_DEX = "dex"


class ExecutorState(Enum):
    IDLE = auto()
    VALIDATING = auto()
    LEG1_PENDING = auto()
    LEG1_FILLED = auto()
    LEG2_PENDING = auto()
    DONE = auto()
    FAILED = auto()
    UNWINDING = auto()


@dataclass
class ExecutionContext:
    """Mutable per-execution state passed through the state machine."""

    signal: Signal
    state: ExecutorState = ExecutorState.IDLE

    leg1_venue: str = ""
    leg1_order_id: Optional[str] = None
    leg1_fill_price: Optional[Decimal] = None
    leg1_fill_size: Optional[Decimal] = None

    leg2_venue: str = ""
    leg2_tx_hash: Optional[str] = None
    leg2_fill_price: Optional[Decimal] = None
    leg2_fill_size: Optional[Decimal] = None

    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    actual_net_pnl: Optional[Decimal] = None
    error: Optional[str] = None
    was_unwound: bool = False

    # Free-form metadata bubbled up from leg implementations. Used today by the
    # dry-run-signed DEX leg to surface the signed raw tx hex, real tx hash
    # (which we deliberately do NOT broadcast), and fork preflight gas usage.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutorConfig:
    leg1_timeout: float = DEFAULT_LEG1_TIMEOUT_S
    leg2_timeout: float = DEFAULT_LEG2_TIMEOUT_S
    min_fill_ratio: Decimal = DEFAULT_MIN_FILL_RATIO
    use_flashbots: bool = False
    simulation_mode: bool = True
    # Live DEX leg when ``simulation_mode`` is False (needs wallet, resolver, pricing).
    dex_slippage_bps: Decimal = Decimal("50")
    dex_deadline_seconds: int = 300
    dex_run_preflight: bool = True
    dex_expected_chain_id: Optional[int] = None
    dex_allow_mainnet: bool = False
    # When True, the DEX leg goes through the live router-calldata path with
    # ``dry_run=True`` so the bot builds + fork-preflights + signs the swap but
    # skips broadcasting. CEX leg behaviour is unaffected (still controlled by
    # ``simulation_mode``).
    dex_dry_run_signed: bool = False

    def __post_init__(self) -> None:
        self.min_fill_ratio = to_decimal(self.min_fill_ratio)
        self.dex_slippage_bps = to_decimal(self.dex_slippage_bps)
        if self.leg1_timeout <= 0 or self.leg2_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not (Decimal("0") < self.min_fill_ratio <= Decimal("1")):
            raise ValueError("min_fill_ratio must be in (0, 1]")
        if self.dex_deadline_seconds < 1:
            raise ValueError("dex_deadline_seconds must be >= 1")
        if not (Decimal("0") <= self.dex_slippage_bps <= Decimal("10000")):
            raise ValueError("dex_slippage_bps must be in [0, 10000]")


class Executor:
    """Execute arbitrage trades across CEX and DEX with full state tracking."""

    def __init__(
        self,
        exchange_client: Any,
        pricing_module: Any,
        inventory_tracker: Any,
        config: Optional[ExecutorConfig] = None,
        *,
        fees: Optional[FeeStructure] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        replay_protection: Optional[ReplayProtection] = None,
        dex_wallet: Any = None,
        dex_token_resolver: Optional[Callable[[str], Any]] = None,
        metrics: Any = None,
    ) -> None:
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.config = config or ExecutorConfig()
        self.fees = fees or FeeStructure()

        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.replay_protection = replay_protection or ReplayProtection()
        self.dex_wallet = dex_wallet
        self.dex_token_resolver = dex_token_resolver
        self.metrics = metrics

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, signal: Signal) -> ExecutionContext:
        """Run both legs (order depends on ``use_flashbots``), return context."""
        t0 = time.monotonic()
        ctx = ExecutionContext(signal=signal)
        result_label = "unknown"
        try:
            if self.circuit_breaker.is_open():
                ctx.state = ExecutorState.FAILED
                ctx.error = "Circuit breaker open"
                ctx.finished_at = time.time()
                result_label = "circuit_open"
                return ctx

            if self.replay_protection.is_duplicate(signal):
                ctx.state = ExecutorState.FAILED
                ctx.error = "Duplicate signal"
                ctx.finished_at = time.time()
                result_label = "duplicate"
                return ctx

            ctx.state = ExecutorState.VALIDATING
            if not signal.is_valid():
                ctx.state = ExecutorState.FAILED
                reasons = ", ".join(signal.invalidity_reasons())
                ctx.error = f"Signal invalid ({reasons})" if reasons else "Signal invalid"
                ctx.finished_at = time.time()
                result_label = "invalid_signal"
                return ctx

            entered_try = False
            try:
                entered_try = True
                if self.config.use_flashbots:
                    ctx = await self._execute_dex_first(ctx)
                else:
                    ctx = await self._execute_cex_first(ctx)
            finally:
                if entered_try:
                    # Mark executed only after we actually attempted the legs so
                    # pre-flight failures do not poison replay or the circuit breaker.
                    self.replay_protection.mark_executed(signal)
                    if ctx.state == ExecutorState.DONE:
                        self.circuit_breaker.record_success()
                    else:
                        self.circuit_breaker.record_failure()
                    ctx.finished_at = time.time()
                    result_label = "done" if ctx.state == ExecutorState.DONE else "failed"

            return ctx
        finally:
            if self.metrics is not None:
                self.metrics.record_execution(result_label, time.monotonic() - t0)

    # ------------------------------------------------------------------
    # Path A: CEX leg first (default when not using Flashbots)
    # ------------------------------------------------------------------

    async def _execute_cex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        signal = ctx.signal

        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = VENUE_CEX
        try:
            leg1 = await asyncio.wait_for(
                self._execute_cex_leg(signal, signal.size),
                timeout=self.config.leg1_timeout,
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = leg1.get("error") or "CEX rejected"
            return ctx

        fill_price = to_decimal(leg1["price"])
        fill_size = to_decimal(leg1["filled"])
        if fill_size / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "Partial fill below threshold"
            return ctx

        ctx.leg1_fill_price = fill_price
        ctx.leg1_fill_size = fill_size
        ctx.state = ExecutorState.LEG1_FILLED

        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = VENUE_DEX
        try:
            leg2 = await asyncio.wait_for(
                self._execute_dex_leg(signal, fill_size),
                timeout=self.config.leg2_timeout,
            )
        except asyncio.TimeoutError:
            await self._run_unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout - unwound"
            return ctx

        if not leg2["success"]:
            await self._run_unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = f"DEX failed - unwound ({leg2.get('error', 'unknown')})"
            self._absorb_leg_metadata(ctx, leg2, leg_label="leg2")
            return ctx

        ctx.leg2_fill_price = to_decimal(leg2["price"])
        ctx.leg2_fill_size = to_decimal(leg2["filled"])
        ctx.leg2_tx_hash = leg2.get("tx_hash") or ctx.leg2_tx_hash
        self._absorb_leg_metadata(ctx, leg2, leg_label="leg2")
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    # ------------------------------------------------------------------
    # Path B: DEX leg first (Flashbots: a failed tx has no cost)
    # ------------------------------------------------------------------

    async def _execute_dex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        signal = ctx.signal

        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = VENUE_DEX
        try:
            leg1 = await asyncio.wait_for(
                self._execute_dex_leg(signal, signal.size),
                timeout=self.config.leg2_timeout,
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX failed (no cost via Flashbots)"
            self._absorb_leg_metadata(ctx, leg1, leg_label="leg1")
            return ctx

        fill_price = to_decimal(leg1["price"])
        fill_size = to_decimal(leg1["filled"])
        if fill_size / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX partial fill below threshold"
            self._absorb_leg_metadata(ctx, leg1, leg_label="leg1")
            return ctx

        ctx.leg1_fill_price = fill_price
        ctx.leg1_fill_size = fill_size
        ctx.leg2_tx_hash = leg1.get("tx_hash") or ctx.leg2_tx_hash
        self._absorb_leg_metadata(ctx, leg1, leg_label="leg1")
        ctx.state = ExecutorState.LEG1_FILLED

        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = VENUE_CEX
        try:
            leg2 = await asyncio.wait_for(
                self._execute_cex_leg(signal, fill_size),
                timeout=self.config.leg1_timeout,
            )
        except asyncio.TimeoutError:
            await self._run_unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout after DEX - unwound"
            return ctx

        if not leg2["success"]:
            await self._run_unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = f"CEX failed after DEX - unwound ({leg2.get('error', 'unknown')})"
            return ctx

        ctx.leg2_fill_price = to_decimal(leg2["price"])
        ctx.leg2_fill_size = to_decimal(leg2["filled"])
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    # ------------------------------------------------------------------
    # Venue-specific leg implementations
    # ------------------------------------------------------------------

    async def _execute_cex_leg(self, signal: Signal, size: Decimal) -> dict[str, Any]:
        """Place one CEX leg (limit IOC). Returns ``{success, price, filled, error?}``."""
        size_d = to_decimal(size)
        if self.config.simulation_mode:
            await asyncio.sleep(SIMULATION_CEX_LATENCY_S)
            return {
                "success": True,
                "price": signal.cex_price * SIMULATION_CEX_PRICE_ADJUST,
                "filled": size_d,
            }
        side = "buy" if signal.direction == Direction.BUY_CEX_SELL_DEX else "sell"
        # Pad limit price so IOC crosses; ExchangeClient floors input to Decimal.
        limit_price = signal.cex_price * CEX_SLIPPAGE_PAD
        result = self.exchange.create_limit_ioc_order(
            symbol=signal.pair,
            side=side,
            amount=float(size_d),
            price=float(limit_price),
        )
        status = result.get("status")
        return {
            "success": status in {"filled", "closed"},
            "price": to_decimal(result.get("avg_fill_price")),
            "filled": to_decimal(result.get("amount_filled")),
            "error": status,
        }

    async def _execute_dex_leg(self, signal: Signal, size: Decimal) -> dict[str, Any]:
        """Place one DEX leg.

        Three modes, in order of precedence:

        1. ``dex_dry_run_signed`` — go through the live router-calldata path
           with ``dry_run=True``: real route, fork preflight, EIP-1559 build,
           signing, but **no broadcast**. Used by ``ARB_DRY_RUN_MODE=signed``.
        2. ``simulation_mode`` and not signed-dry-run — pure math simulation,
           no wallet / RPC required.
        3. Otherwise — live broadcast via :func:`sync_execute_live_dex_leg`.
        """
        size_d = to_decimal(size)
        if self.config.dex_dry_run_signed:
            if self.dex_wallet is None or self.dex_token_resolver is None or self.pricing is None:
                return {
                    "success": False,
                    "price": signal.dex_price,
                    "filled": Decimal("0"),
                    "error": "dry_run_signed_requires_wallet_resolver_pricing",
                    "dry_run": True,
                }
            from executor.live_dex_leg import sync_execute_live_dex_leg

            return await asyncio.to_thread(
                sync_execute_live_dex_leg,
                pricing_engine=self.pricing,
                wallet=self.dex_wallet,
                token_resolver=self.dex_token_resolver,
                signal=signal,
                size_base_human=size_d,
                direction=signal.direction,
                slippage_bps=self.config.dex_slippage_bps,
                deadline_seconds=self.config.dex_deadline_seconds,
                run_preflight=self.config.dex_run_preflight,
                expected_chain_id=self.config.dex_expected_chain_id,
                allow_mainnet=self.config.dex_allow_mainnet,
                dry_run=True,
            )

        if self.config.simulation_mode:
            await asyncio.sleep(SIMULATION_DEX_LATENCY_S)
            return {
                "success": True,
                "price": signal.dex_price * SIMULATION_DEX_PRICE_ADJUST,
                "filled": size_d,
            }
        if self.dex_wallet is None or self.dex_token_resolver is None:
            return {
                "success": False,
                "price": signal.dex_price,
                "filled": Decimal("0"),
                "error": "dex_wallet_or_resolver_missing",
            }
        if self.pricing is None:
            return {
                "success": False,
                "price": signal.dex_price,
                "filled": Decimal("0"),
                "error": "pricing_missing",
            }

        from executor.live_dex_leg import sync_execute_live_dex_leg

        return await asyncio.to_thread(
            sync_execute_live_dex_leg,
            pricing_engine=self.pricing,
            wallet=self.dex_wallet,
            token_resolver=self.dex_token_resolver,
            signal=signal,
            size_base_human=size_d,
            direction=signal.direction,
            slippage_bps=self.config.dex_slippage_bps,
            deadline_seconds=self.config.dex_deadline_seconds,
            run_preflight=self.config.dex_run_preflight,
            expected_chain_id=self.config.dex_expected_chain_id,
            allow_mainnet=self.config.dex_allow_mainnet,
        )

    async def _run_unwind(self, ctx: ExecutionContext) -> None:
        ctx.state = ExecutorState.UNWINDING
        ctx.was_unwound = True
        await self._unwind(ctx)

    async def _unwind(self, ctx: ExecutionContext) -> None:
        """Flatten a stuck post-leg-1 position with a market order on the CEX side.

        In simulation this is a no-op that just logs. In live mode we reverse
        whatever leg1 was by hitting the CEX market.
        """
        if self.config.simulation_mode:
            logger.info(
                "UNWIND simulation: would flatten %s size=%s",
                ctx.leg1_venue,
                ctx.leg1_fill_size,
            )
            await asyncio.sleep(SIMULATION_UNWIND_LATENCY_S)
            return
        if ctx.leg1_fill_size is None or ctx.leg1_venue != VENUE_CEX:
            logger.warning("UNWIND: no CEX leg1 to reverse (venue=%s)", ctx.leg1_venue)
            return
        reverse_side = "sell" if ctx.signal.direction == Direction.BUY_CEX_SELL_DEX else "buy"
        try:
            self.exchange.create_market_order(
                symbol=ctx.signal.pair,
                side=reverse_side,
                amount=float(ctx.leg1_fill_size),
            )
        except Exception as exc:
            logger.exception("UNWIND failed: %s", exc)

    def _absorb_leg_metadata(
        self,
        ctx: ExecutionContext,
        leg_result: dict[str, Any],
        *,
        leg_label: str,
    ) -> None:
        """Copy non-PnL leg fields (signed raw tx, gas used, dry_run flag) onto ``ctx.metadata``.

        The DEX dry-run-signed path uses this to surface the *real* tx hash and
        signed payload so the bot can log them, write them to CSV, and Telegram
        them — without ever broadcasting.
        """
        keys = (
            "dry_run",
            "signed_raw_tx_hex",
            "signed_tx_hash",
            "preflight_gas_used",
            "router",
        )
        for key in keys:
            if key in leg_result and leg_result[key] is not None:
                ctx.metadata[f"{leg_label}_{key}"] = leg_result[key]

    def _calculate_pnl(self, ctx: ExecutionContext) -> Decimal:
        """Compute net PnL from filled legs using the same :class:`FeeStructure`."""
        if ctx.leg1_fill_price is None or ctx.leg2_fill_price is None or ctx.leg1_fill_size is None:
            return Decimal("0")
        signal = ctx.signal
        size = ctx.leg1_fill_size
        # Resolve CEX- vs DEX-side fill prices regardless of which leg ran first.
        cex_price = ctx.leg1_fill_price if ctx.leg1_venue == VENUE_CEX else ctx.leg2_fill_price
        dex_price = ctx.leg1_fill_price if ctx.leg1_venue == VENUE_DEX else ctx.leg2_fill_price
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            gross = (dex_price - cex_price) * size
        else:
            gross = (cex_price - dex_price) * size
        trade_value = size * signal.cex_price
        return gross - self.fees.total_fee_usd(trade_value)
