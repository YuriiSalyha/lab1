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
from typing import Any, Optional

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


@dataclass
class ExecutorConfig:
    leg1_timeout: float = DEFAULT_LEG1_TIMEOUT_S
    leg2_timeout: float = DEFAULT_LEG2_TIMEOUT_S
    min_fill_ratio: Decimal = DEFAULT_MIN_FILL_RATIO
    use_flashbots: bool = False
    simulation_mode: bool = True

    def __post_init__(self) -> None:
        self.min_fill_ratio = to_decimal(self.min_fill_ratio)
        if self.leg1_timeout <= 0 or self.leg2_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not (Decimal("0") < self.min_fill_ratio <= Decimal("1")):
            raise ValueError("min_fill_ratio must be in (0, 1]")


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
    ) -> None:
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.config = config or ExecutorConfig()
        self.fees = fees or FeeStructure()

        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.replay_protection = replay_protection or ReplayProtection()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, signal: Signal) -> ExecutionContext:
        """Run both legs (order depends on ``use_flashbots``), return context."""
        ctx = ExecutionContext(signal=signal)

        if self.circuit_breaker.is_open():
            ctx.state = ExecutorState.FAILED
            ctx.error = "Circuit breaker open"
            ctx.finished_at = time.time()
            return ctx

        if self.replay_protection.is_duplicate(signal):
            ctx.state = ExecutorState.FAILED
            ctx.error = "Duplicate signal"
            ctx.finished_at = time.time()
            return ctx

        ctx.state = ExecutorState.VALIDATING
        if not signal.is_valid():
            ctx.state = ExecutorState.FAILED
            reasons = ", ".join(signal.invalidity_reasons())
            ctx.error = f"Signal invalid ({reasons})" if reasons else "Signal invalid"
            ctx.finished_at = time.time()
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

        return ctx

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
            return ctx

        ctx.leg2_fill_price = to_decimal(leg2["price"])
        ctx.leg2_fill_size = to_decimal(leg2["filled"])
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
            return ctx

        fill_price = to_decimal(leg1["price"])
        fill_size = to_decimal(leg1["filled"])
        if fill_size / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX partial fill below threshold"
            return ctx

        ctx.leg1_fill_price = fill_price
        ctx.leg1_fill_size = fill_size
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
        """Place one DEX leg. Simulation-only unless a real swap executor is wired."""
        size_d = to_decimal(size)
        if self.config.simulation_mode:
            await asyncio.sleep(SIMULATION_DEX_LATENCY_S)
            return {
                "success": True,
                "price": signal.dex_price * SIMULATION_DEX_PRICE_ADJUST,
                "filled": size_d,
            }
        raise NotImplementedError("Real DEX execution requires a live swap executor")

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
