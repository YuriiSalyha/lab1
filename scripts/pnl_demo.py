#!/usr/bin/env python3
"""Print a PnL summary from five synthetic arb records (demo / teaching)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.tracker import Venue


def _make_trade(i: int, buy_px: int, sell_px: int) -> ArbRecord:
    ts = datetime(2026, 1, 1, 12, i, tzinfo=timezone.utc)
    buy = TradeLeg(
        id=f"b{i}",
        timestamp=ts,
        venue=Venue.BINANCE,
        symbol="ETH/USDT",
        side="buy",
        amount=Decimal("1"),
        price=Decimal(buy_px),
        fee=Decimal("1"),
        fee_asset="USDT",
    )
    sell = TradeLeg(
        id=f"s{i}",
        timestamp=ts,
        venue=Venue.WALLET,
        symbol="ETH/USDT",
        side="sell",
        amount=Decimal("1"),
        price=Decimal(sell_px),
        fee=Decimal("1"),
        fee_asset="USDT",
    )
    return ArbRecord(
        id=f"arb{i}",
        timestamp=ts,
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("0.5"),
    )


def main() -> None:
    eng = PnLEngine()
    pairs = [(2000, 2010), (1990, 2005), (2010, 2000), (1980, 1995), (2005, 2020)]
    for i, (bp, sp) in enumerate(pairs):
        eng.record(_make_trade(i, bp, sp))
    s = eng.summary()
    print("PnL Summary (5 synthetic trades)")
    print(f"  Total trades:   {s['total_trades']}")
    print(f"  Total PnL USD:  {s['total_pnl_usd']}")
    print(f"  Win rate:       {s['win_rate'] * 100:.1f}%")
    print(f"  Total fees:     {s['total_fees_usd']}")
    print("Recent:")
    for r in eng.recent(5):
        print(f"  {r}")


if __name__ == "__main__":
    main()
