"""Append-only CSV journal of trade attempts (dry-run, success, failure) for analysis."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from executor.engine import ExecutionContext
from inventory.pnl import ArbRecord
from monitoring.health_state import TradeMetrics
from strategy.signal import Signal

logger = logging.getLogger(__name__)

_ENV_TRADE_CSV = "ARB_TRADE_CSV"
_ENV_TRADE_CSV_DISABLED = "ARB_TRADE_CSV_DISABLED"
_DEFAULT_TRADE_CSV = Path("logs") / "trades_journal.csv"

_TRADE_CSV_FIELDNAMES = [
    "event_ts_utc",
    "outcome",
    "pair",
    "signal_id",
    "direction",
    "size_base",
    "cex_price_signal",
    "dex_price_signal",
    "spread_bps_signal",
    "expected_gross_pnl_usd",
    "expected_fees_usd",
    "expected_net_pnl_usd",
    "score",
    "signal_timestamp_unix",
    "signal_expiry_unix",
    "signal_age_sec_at_event",
    "inventory_ok",
    "within_limits",
    "dry_run",
    "demo",
    "simulation",
    "production_binance",
    "min_score",
    "tick_seconds",
    "executor_state",
    "actual_net_pnl_usd",
    "error_message",
    "started_at_unix",
    "finished_at_unix",
    "duration_exec_ms",
    "leg1_venue",
    "leg1_fill_price",
    "leg1_fill_size",
    "leg2_venue",
    "leg2_fill_price",
    "leg2_fill_size",
    "leg2_tx_hash",
    "gross_pnl_usd",
    "total_fees_usd",
    "gas_cost_usd",
    "net_pnl_usd",
    "net_pnl_bps",
    "notional_usd",
    "buy_venue",
    "sell_venue",
    "buy_amount",
    "buy_price",
    "sell_amount",
    "sell_price",
    "tm_expected_spread_bps",
    "tm_actual_spread_bps",
    "tm_slippage_bps",
    "tm_signal_to_fill_ms",
    "tm_leg1_ms",
    "tm_leg2_ms",
    "tm_gross_pnl",
    "tm_fees_paid",
    "tm_net_pnl",
    "cumulative_session_pnl_usd",
    "balance_verify",
    "metadata_json",
]


def trade_csv_path() -> Path | None:
    if os.getenv(_ENV_TRADE_CSV_DISABLED, "").strip().lower() in ("1", "true", "yes"):
        return None
    raw = os.getenv(_ENV_TRADE_CSV, "").strip()
    return Path(raw) if raw else _DEFAULT_TRADE_CSV


class TradeCsvJournal:
    """One row per trade attempt; safe to call from the asyncio bot loop."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def append_row(self, row: dict[str, str]) -> None:
        if not self.enabled or self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self._path.exists() or self._path.stat().st_size == 0
            with self._path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_TRADE_CSV_FIELDNAMES, extrasaction="ignore")
                if new_file:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in _TRADE_CSV_FIELDNAMES})
        except OSError as exc:
            logger.warning("trade CSV append failed: %s", exc)


def _s(d: Decimal | None) -> str:
    if d is None:
        return ""
    return str(d)


def _meta_json(metadata: dict[str, Any]) -> str:
    try:
        s = json.dumps(metadata, default=str, separators=(",", ":"))
    except TypeError:
        s = "{}"
    return s[:4000]


def build_trade_csv_row(
    *,
    outcome: str,
    pair: str,
    signal: Signal,
    event_mono: float,
    config_demo: bool,
    config_dry_run: bool,
    config_simulation: bool,
    production_binance: bool,
    min_score: Decimal,
    tick_seconds: float,
    ctx: ExecutionContext | None = None,
    arb_record: ArbRecord | None = None,
    tm: TradeMetrics | None = None,
    cumulative_session_pnl: Decimal | None = None,
    balance_verify: str = "",
    error_message: str = "",
) -> dict[str, str]:
    age = max(0.0, event_mono - float(signal.timestamp))
    row: dict[str, str] = {
        "event_ts_utc": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "pair": pair,
        "signal_id": signal.signal_id,
        "direction": signal.direction.value,
        "size_base": _s(signal.size),
        "cex_price_signal": _s(signal.cex_price),
        "dex_price_signal": _s(signal.dex_price),
        "spread_bps_signal": _s(signal.spread_bps),
        "expected_gross_pnl_usd": _s(signal.expected_gross_pnl),
        "expected_fees_usd": _s(signal.expected_fees),
        "expected_net_pnl_usd": _s(signal.expected_net_pnl),
        "score": _s(signal.score),
        "signal_timestamp_unix": str(signal.timestamp),
        "signal_expiry_unix": str(signal.expiry),
        "signal_age_sec_at_event": f"{age:.6f}",
        "inventory_ok": str(signal.inventory_ok).lower(),
        "within_limits": str(signal.within_limits).lower(),
        "dry_run": str(config_dry_run).lower(),
        "demo": str(config_demo).lower(),
        "simulation": str(config_simulation).lower(),
        "production_binance": str(production_binance).lower(),
        "min_score": _s(min_score),
        "tick_seconds": str(tick_seconds),
        "metadata_json": _meta_json(signal.metadata),
    }
    if ctx is not None:
        row["executor_state"] = ctx.state.name
        row["error_message"] = error_message or (ctx.error or "")
        row["started_at_unix"] = str(ctx.started_at)
        fin = ctx.finished_at if ctx.finished_at is not None else ""
        row["finished_at_unix"] = str(fin) if fin != "" else ""
        if ctx.finished_at is not None:
            row["duration_exec_ms"] = str(int(max(0.0, (ctx.finished_at - ctx.started_at) * 1000)))
        else:
            row["duration_exec_ms"] = ""
        row["leg1_venue"] = ctx.leg1_venue
        row["leg1_fill_price"] = _s(ctx.leg1_fill_price)
        row["leg1_fill_size"] = _s(ctx.leg1_fill_size)
        row["leg2_venue"] = ctx.leg2_venue
        row["leg2_fill_price"] = _s(ctx.leg2_fill_price)
        row["leg2_fill_size"] = _s(ctx.leg2_fill_size)
        row["leg2_tx_hash"] = ctx.leg2_tx_hash or ""
        row["actual_net_pnl_usd"] = _s(ctx.actual_net_pnl)
    else:
        row["executor_state"] = ""
        row["error_message"] = error_message
        row["started_at_unix"] = ""
        row["finished_at_unix"] = ""
        row["duration_exec_ms"] = ""
        row["leg1_venue"] = ""
        row["leg1_fill_price"] = ""
        row["leg1_fill_size"] = ""
        row["leg2_venue"] = ""
        row["leg2_fill_price"] = ""
        row["leg2_fill_size"] = ""
        row["leg2_tx_hash"] = ""
        row["actual_net_pnl_usd"] = ""

    if arb_record is not None:
        row["gross_pnl_usd"] = _s(arb_record.gross_pnl)
        row["total_fees_usd"] = _s(arb_record.total_fees)
        row["gas_cost_usd"] = _s(arb_record.gas_cost_usd)
        row["net_pnl_usd"] = _s(arb_record.net_pnl)
        row["net_pnl_bps"] = _s(arb_record.net_pnl_bps)
        row["notional_usd"] = _s(arb_record.notional)
        row["buy_venue"] = arb_record.buy_leg.venue.value
        row["sell_venue"] = arb_record.sell_leg.venue.value
        row["buy_amount"] = _s(arb_record.buy_leg.amount)
        row["buy_price"] = _s(arb_record.buy_leg.price)
        row["sell_amount"] = _s(arb_record.sell_leg.amount)
        row["sell_price"] = _s(arb_record.sell_leg.price)
    else:
        for k in (
            "gross_pnl_usd",
            "total_fees_usd",
            "gas_cost_usd",
            "net_pnl_usd",
            "net_pnl_bps",
            "notional_usd",
            "buy_venue",
            "sell_venue",
            "buy_amount",
            "buy_price",
            "sell_amount",
            "sell_price",
        ):
            row[k] = ""

    if tm is not None:
        row["tm_expected_spread_bps"] = _s(tm.expected_spread_bps)
        row["tm_actual_spread_bps"] = (
            "" if tm.actual_spread_bps is None else _s(tm.actual_spread_bps)
        )
        row["tm_slippage_bps"] = _s(tm.slippage_bps)
        row["tm_signal_to_fill_ms"] = str(tm.signal_to_fill_ms)
        row["tm_leg1_ms"] = str(tm.leg1_time_ms)
        row["tm_leg2_ms"] = str(tm.leg2_time_ms)
        row["tm_gross_pnl"] = _s(tm.gross_pnl)
        row["tm_fees_paid"] = _s(tm.fees_paid)
        row["tm_net_pnl"] = _s(tm.net_pnl)
    else:
        for k in (
            "tm_expected_spread_bps",
            "tm_actual_spread_bps",
            "tm_slippage_bps",
            "tm_signal_to_fill_ms",
            "tm_leg1_ms",
            "tm_leg2_ms",
            "tm_gross_pnl",
            "tm_fees_paid",
            "tm_net_pnl",
        ):
            row[k] = ""

    row["cumulative_session_pnl_usd"] = (
        _s(cumulative_session_pnl) if cumulative_session_pnl is not None else ""
    )
    row["balance_verify"] = balance_verify
    return row


def production_flag_best_effort() -> bool:
    try:
        from config.config import PRODUCTION

        return bool(PRODUCTION)
    except Exception:
        return False
