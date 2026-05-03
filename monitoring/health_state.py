"""In-process health and per-trade metrics (money fields use Decimal)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from executor.engine import ExecutionContext
from inventory.pnl import ArbRecord
from strategy.signal import Signal


@dataclass
class BotHealth:
    """Snapshot fields updated by the bot loop (not thread-safe; asyncio single-task)."""

    is_running: bool = True
    last_heartbeat: float = 0.0
    last_trade_time: float = 0.0
    cex_connected: bool = False
    cex_last_response_ms: int = 0
    current_capital: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    error_count_1h: int = 0
    circuit_breaker_open: bool = False
    _error_mono: deque[float] = field(default_factory=deque, repr=False)

    def touch_heartbeat(self, *, time_fn: Callable[[], float] | None = None) -> None:
        fn = time_fn or time.time
        self.last_heartbeat = float(fn())

    def record_error(self, *, time_fn: Callable[[], float] | None = None) -> None:
        fn = time_fn or time.time
        now = float(fn())
        self._error_mono.append(now)
        self._prune_errors(time_fn=fn)
        self.error_count_1h = len(self._error_mono)

    def _prune_errors(self, *, time_fn: Callable[[], float]) -> None:
        now = float(time_fn())
        while self._error_mono and (now - self._error_mono[0]) > 3600.0:
            self._error_mono.popleft()


@dataclass(frozen=True)
class TradeMetrics:
    expected_spread_bps: Decimal
    actual_spread_bps: Decimal | None
    slippage_bps: Decimal
    signal_to_fill_ms: int
    leg1_time_ms: int
    leg2_time_ms: int
    gross_pnl: Decimal
    fees_paid: Decimal
    net_pnl: Decimal


def build_trade_metrics(
    signal: Signal,
    ctx: ExecutionContext,
    record: ArbRecord | None,
) -> TradeMetrics:
    """Derive metrics from signal + execution context + optional :class:`ArbRecord`."""
    expected_net = signal.expected_net_pnl
    actual_net = ctx.actual_net_pnl if ctx.actual_net_pnl is not None else Decimal("0")
    tw = signal.size * signal.cex_price
    if tw > 0:
        slippage_bps = (expected_net - actual_net) / tw * Decimal("10000")
    else:
        slippage_bps = Decimal("0")
    finished = ctx.finished_at or ctx.started_at
    signal_to_fill_ms = max(0, int((finished - signal.timestamp) * 1000))
    total_exec_ms = max(0, int((finished - ctx.started_at) * 1000))
    leg1_ms = total_exec_ms // 2
    leg2_ms = total_exec_ms - leg1_ms

    if record is not None:
        gross = record.gross_pnl
        fees = record.total_fees
        actual_spread_bps: Decimal | None = record.net_pnl_bps
    else:
        gross = actual_net
        fees = Decimal("0")
        actual_spread_bps = None

    return TradeMetrics(
        expected_spread_bps=signal.spread_bps,
        actual_spread_bps=actual_spread_bps,
        slippage_bps=slippage_bps,
        signal_to_fill_ms=signal_to_fill_ms,
        leg1_time_ms=leg1_ms,
        leg2_time_ms=leg2_ms,
        gross_pnl=gross,
        fees_paid=fees,
        net_pnl=actual_net,
    )


def format_trade_metrics_log(m: TradeMetrics) -> str:
    """Pipe-separated single line for grep."""
    asp = "na" if m.actual_spread_bps is None else str(m.actual_spread_bps)
    return (
        f"expected_spread_bps={m.expected_spread_bps}|actual_spread_bps={asp}|"
        f"slippage_bps={m.slippage_bps}|signal_to_fill_ms={m.signal_to_fill_ms}|"
        f"leg1_ms={m.leg1_time_ms}|leg2_ms={m.leg2_time_ms}|gross_pnl={m.gross_pnl}|"
        f"fees_paid={m.fees_paid}|net_pnl={m.net_pnl}"
    )
