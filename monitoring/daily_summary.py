"""End-of-day style summary from :class:`~inventory.pnl.PnLEngine` records."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from inventory.pnl import ArbRecord, PnLEngine


def _utc_day(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


def trades_on_utc_day(trades: list[ArbRecord], day: str | None = None) -> list[ArbRecord]:
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [t for t in trades if _utc_day(t.timestamp) == day]


def generate_daily_summary(
    pnl_engine: PnLEngine,
    *,
    current_capital: Decimal | None = None,
    day: str | None = None,
) -> str:
    """Plain-text summary (caller may wrap for Telegram HTML)."""
    trades = trades_on_utc_day(pnl_engine.trades, day)
    if not trades:
        d = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"Daily Summary: No trades on UTC {d}."

    total_net = sum((t.net_pnl for t in trades), Decimal("0"))
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = len(trades) - wins
    best = max(trades, key=lambda t: t.net_pnl)
    worst = min(trades, key=lambda t: t.net_pnl)
    win_rate_pct = (Decimal(wins) / Decimal(len(trades)) * Decimal("100")).quantize(Decimal("1"))
    cap_line = ""
    if current_capital is not None:
        cap_line = f"\nCapital (est): {current_capital}"

    return (
        f"Daily Summary (UTC {day or _utc_day(trades[0].timestamp)})\n"
        f"Trades: {len(trades)} ({wins}W / {losses}L) win_rate~{win_rate_pct}%\n"
        f"Net PnL: {total_net}\n"
        f"Best: {best.net_pnl}  Worst: {worst.net_pnl}"
        f"{cap_line}"
    )
