# inventory/pnl.py

from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from inventory.tracker import Venue

# Fee → USD (estimation; align with rebalancer reference prices)
REFERENCE_USD_PER_ETH = Decimal("2000")
REFERENCE_USD_PER_STABLE = Decimal("1")


def _fee_to_usd(fee: Decimal, fee_asset: str) -> Decimal:
    if fee_asset in ("USDT", "USDC", "BUSD"):
        return fee * REFERENCE_USD_PER_STABLE
    if fee_asset == "ETH":
        return fee * REFERENCE_USD_PER_ETH
    return fee * REFERENCE_USD_PER_STABLE


@dataclass
class TradeLeg:
    """Single execution leg."""

    id: str
    timestamp: datetime
    venue: Venue
    symbol: str  # "ETH/USDT"
    side: str  # "buy" or "sell"
    amount: Decimal  # Base asset qty
    price: Decimal  # Execution price (quote per base)
    fee: Decimal
    fee_asset: str


@dataclass
class ArbRecord:
    """Complete arb trade with both legs."""

    id: str
    timestamp: datetime
    buy_leg: TradeLeg
    sell_leg: TradeLeg
    gas_cost_usd: Decimal = Decimal("0")

    @property
    def _buy_quote(self) -> Decimal:
        return self.buy_leg.amount * self.buy_leg.price

    @property
    def _sell_quote(self) -> Decimal:
        return self.sell_leg.amount * self.sell_leg.price

    @property
    def gross_pnl(self) -> Decimal:
        """Sell revenue minus buy cost (quote currency)."""
        return self._sell_quote - self._buy_quote

    @property
    def total_fees(self) -> Decimal:
        """All fees: both legs (USD) + gas (USD)."""
        b = _fee_to_usd(self.buy_leg.fee, self.buy_leg.fee_asset)
        s = _fee_to_usd(self.sell_leg.fee, self.sell_leg.fee_asset)
        return b + s + self.gas_cost_usd

    @property
    def net_pnl(self) -> Decimal:
        """Gross minus fees (USD)."""
        return self.gross_pnl - self.total_fees

    @property
    def notional(self) -> Decimal:
        """Trade size in quote: average of buy and sell leg notionals."""
        return (self._buy_quote + self._sell_quote) / Decimal("2")

    @property
    def net_pnl_bps(self) -> Decimal:
        """Net PnL in basis points of notional."""
        n = self.notional
        if n <= 0:
            return Decimal("0")
        return self.net_pnl / n * Decimal("10000")


class PnLEngine:
    """
    Tracks all arb trades and produces PnL reports.
    """

    def __init__(self):
        self.trades: list[ArbRecord] = []

    def record(self, trade: ArbRecord):
        """Record a completed arb trade."""
        self.trades.append(trade)

    def summary(self) -> dict:
        """
        Aggregate PnL summary.
        """
        n = len(self.trades)
        if n == 0:
            return {
                "total_trades": 0,
                "total_pnl_usd": Decimal("0"),
                "total_fees_usd": Decimal("0"),
                "avg_pnl_per_trade": Decimal("0"),
                "avg_pnl_bps": Decimal("0"),
                "win_rate": 0.0,
                "best_trade_pnl": Decimal("0"),
                "worst_trade_pnl": Decimal("0"),
                "total_notional": Decimal("0"),
                "sharpe_estimate": 0.0,
                "pnl_by_hour": {},
            }

        pnls = [t.net_pnl for t in self.trades]
        fees = [t.total_fees for t in self.trades]
        notionals = [t.notional for t in self.trades]
        total_pnl = sum(pnls, Decimal("0"))
        total_fees_usd = sum(fees, Decimal("0"))
        total_notional = sum(notionals, Decimal("0"))
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / n
        avg_pnl = total_pnl / n
        bps_list = [t.net_pnl_bps for t in self.trades]
        avg_bps = sum(bps_list, Decimal("0")) / Decimal(n)

        pnl_floats = [float(p) for p in pnls]
        if len(pnl_floats) >= 2:
            stdev = statistics.pstdev(pnl_floats)
            mean = statistics.mean(pnl_floats)
            sharpe = mean / stdev if stdev > 0 else 0.0
        else:
            sharpe = 0.0

        pnl_by_hour: dict[int, Decimal] = {}
        for t in self.trades:
            h = t.timestamp.hour
            pnl_by_hour[h] = pnl_by_hour.get(h, Decimal("0")) + t.net_pnl

        return {
            "total_trades": n,
            "total_pnl_usd": total_pnl,
            "total_fees_usd": total_fees_usd,
            "avg_pnl_per_trade": avg_pnl,
            "avg_pnl_bps": avg_bps,
            "win_rate": win_rate,
            "best_trade_pnl": max(pnls),
            "worst_trade_pnl": min(pnls),
            "total_notional": total_notional,
            "sharpe_estimate": sharpe,
            "pnl_by_hour": pnl_by_hour,
        }

    def recent(self, n: int = 10) -> list[dict]:
        """
        Last N trades as summary dicts.
        """
        out: list[dict] = []
        for t in self.trades[-n:]:
            out.append(
                {
                    "id": t.id,
                    "timestamp": t.timestamp,
                    "symbol": t.buy_leg.symbol,
                    "buy_venue": t.buy_leg.venue.value,
                    "sell_venue": t.sell_leg.venue.value,
                    "net_pnl_usd": t.net_pnl,
                    "net_pnl_bps": t.net_pnl_bps,
                    "profitable": t.net_pnl > 0,
                }
            )
        return out

    def export_csv(self, filepath: str):
        """Export all trades to CSV for analysis."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "id",
            "timestamp",
            "gross_pnl_usd",
            "total_fees_usd",
            "gas_cost_usd",
            "net_pnl_usd",
            "net_pnl_bps",
            "notional_usd",
            "buy_leg_id",
            "buy_venue",
            "sell_leg_id",
            "sell_venue",
            "symbol",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for t in self.trades:
                w.writerow(
                    {
                        "id": t.id,
                        "timestamp": t.timestamp.isoformat(),
                        "gross_pnl_usd": str(t.gross_pnl),
                        "total_fees_usd": str(t.total_fees),
                        "gas_cost_usd": str(t.gas_cost_usd),
                        "net_pnl_usd": str(t.net_pnl),
                        "net_pnl_bps": str(t.net_pnl_bps),
                        "notional_usd": str(t.notional),
                        "buy_leg_id": t.buy_leg.id,
                        "buy_venue": t.buy_leg.venue.value,
                        "sell_leg_id": t.sell_leg.id,
                        "sell_venue": t.sell_leg.venue.value,
                        "symbol": t.buy_leg.symbol,
                    }
                )


def _print_summary(engine: PnLEngine, last_n: int) -> None:
    s = engine.summary()
    print()
    print("PnL Summary (last 24h)")
    print("═" * 43)
    print(f"Total Trades:        {s['total_trades']}")
    print(f"Win Rate:            {s['win_rate'] * 100:.1f}%")
    print(f"Total PnL:           ${s['total_pnl_usd']:.2f}")
    print(f"Total Fees:          ${s['total_fees_usd']:.2f}")
    print(f"Avg PnL/Trade:       ${s['avg_pnl_per_trade']:.2f}")
    print(f"Avg PnL (bps):       {s['avg_pnl_bps']:.1f} bps")
    print(f"Best Trade:          ${s['best_trade_pnl']:.2f}")
    print(f"Worst Trade:         ${s['worst_trade_pnl']:.2f}")
    print(f"Total Notional:      ${s['total_notional']:,.0f}")
    print()
    print("Recent Trades:")
    for row in engine.recent(last_n):
        ts = row["timestamp"].strftime("%H:%M")
        sym = row["symbol"]
        b = row["buy_venue"]
        s_ = row["sell_venue"]
        pnl = row["net_pnl_usd"]
        bps = row["net_pnl_bps"]
        mark = "OK" if row["profitable"] else "X"
        sign = "+" if pnl >= 0 else ""
        print(
            f"  {ts}  {sym}  Buy {b} / Sell {s_}  "
            f"{sign}${pnl:.2f} ({float(bps):.1f} bps) [{mark}]"
        )
    print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="PnL summary (in-memory engine is empty unless wired up)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary",
    )
    parser.add_argument("--last-n", type=int, default=10, metavar="N", help="Recent trades to show")
    args = parser.parse_args(argv)
    if args.summary:
        _print_summary(PnLEngine(), args.last_n)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
