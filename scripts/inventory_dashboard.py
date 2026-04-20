#!/usr/bin/env python3
"""Live inventory dashboard: Binance + Bybit + on-chain wallet (Rich terminal UI)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient  # noqa: E402
from config.config import BINANCE_CONFIG, BYBIT_CONFIG  # noqa: E402
from core.types import Address  # noqa: E402
from core.wallet import WalletManager  # noqa: E402
from exchange.client import ExchangeClient  # noqa: E402
from inventory.tracker import InventoryTracker, Venue  # noqa: E402

# --- Configuration (no magic numbers in body) ---
POLL_INTERVAL_SEC = 5.0
TABLE_MIN_WIDTH = 88
REFRESH_PER_SECOND_MIN = 0.2
RPC_ENV_KEYS = ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT", "SEPOLIA_RPC")
PRIVATE_KEY_ENV = "PRIVATE_KEY"


def _rpc(cli: str | None) -> str | None:
    if cli and cli.strip():
        return cli.strip()
    for key in RPC_ENV_KEYS:
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


def _build_tracker(
    *,
    include_binance: bool,
    include_bybit: bool,
    include_wallet: bool,
) -> InventoryTracker:
    venues: list[Venue] = []
    if include_binance:
        venues.append(Venue.BINANCE)
    if include_bybit:
        venues.append(Venue.BYBIT)
    if include_wallet:
        venues.append(Venue.WALLET)
    if not venues:
        raise SystemExit("Enable at least one venue (see --help).")
    return InventoryTracker(venues)


def _poll_balances(tracker: InventoryTracker, rpc: str | None = None) -> list[str]:
    notes: list[str] = []
    if Venue.BINANCE in tracker.venues:
        try:
            xc = ExchangeClient(BINANCE_CONFIG, exchange_id="binance")
            tracker.update_from_cex(Venue.BINANCE, xc.fetch_balance())
        except Exception as e:
            notes.append(f"binance: {e}")
    if Venue.BYBIT in tracker.venues:
        try:
            xb = ExchangeClient(BYBIT_CONFIG, exchange_id="bybit")
            tracker.update_from_cex(Venue.BYBIT, xb.fetch_balance())
        except Exception as e:
            notes.append(f"bybit: {e}")
    if Venue.WALLET in tracker.venues:
        pk = os.getenv(PRIVATE_KEY_ENV, "").strip()
        if not pk:
            notes.append("wallet: PRIVATE_KEY not set")
        elif not rpc:
            notes.append("wallet: no RPC (set --rpc or MAINNET_RPC / …)")
        else:
            try:
                wm = WalletManager.from_env(PRIVATE_KEY_ENV)
                chain = ChainClient([rpc])
                bal = chain.get_balance(Address.from_string(wm.address))
                tracker.update_from_wallet(Venue.WALLET, {"ETH": bal.human})
            except Exception as e:
                notes.append(f"wallet: {e}")
    return notes


def _render_table(tracker: InventoryTracker, notes: list[str]) -> str:
    from rich.console import Console
    from rich.table import Table

    snap = tracker.snapshot()
    ts: datetime = snap["timestamp"]
    table = Table(
        title=f"Inventory — {ts.isoformat()} UTC",
        min_width=TABLE_MIN_WIDTH,
        show_lines=True,
    )
    table.add_column("Venue", style="cyan", no_wrap=True)
    table.add_column("Asset", style="green")
    table.add_column("Free", justify="right")
    table.add_column("Locked", justify="right")
    table.add_column("Total", justify="right")

    venues = snap.get("venues") or {}
    for vname in sorted(venues.keys()):
        assets = venues[vname] or {}
        for asset in sorted(assets.keys()):
            row = assets[asset]
            if not isinstance(row, dict):
                continue
            table.add_row(
                vname,
                asset,
                str(row.get("free", "")),
                str(row.get("locked", "")),
                str(row.get("total", "")),
            )
    totals = snap.get("totals") or {}
    if totals:
        table.add_section()
        for asset in sorted(totals.keys()):
            table.add_row("TOTAL", asset, "", "", str(totals[asset]))

    console = Console(record=True, width=120)
    console.print(table)
    out = console.export_text()
    if notes:
        out += "\nNotes:\n" + "\n".join(f"  • {n}" for n in notes)
    return out


async def _run_live(
    tracker: InventoryTracker,
    rpc: str | None,
    interval: float,
) -> None:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel

    interval = max(interval, REFRESH_PER_SECOND_MIN)
    console = Console()
    with Live(console=console, refresh_per_second=4) as live:
        while True:
            notes = await asyncio.to_thread(_poll_balances, tracker, rpc)
            panel = Panel(
                _render_table(tracker, notes),
                title="[bold]Portfolio[/bold]",
                expand=False,
            )
            live.update(panel)
            await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Live inventory dashboard (CEX + wallet)")
    p.add_argument("--rpc", default=None, help="HTTP RPC for wallet ETH balance")
    p.add_argument(
        "--interval",
        type=float,
        default=POLL_INTERVAL_SEC,
        help=f"Refresh interval seconds (default {POLL_INTERVAL_SEC})",
    )
    p.add_argument(
        "--no-binance",
        action="store_true",
        help="Exclude Binance (default: include)",
    )
    p.add_argument(
        "--no-bybit",
        action="store_true",
        help="Exclude Bybit (default: include)",
    )
    p.add_argument("--wallet", action="store_true", help="Include on-chain ETH wallet")
    args = p.parse_args(argv)

    include_binance = not args.no_binance
    include_bybit = not args.no_bybit
    include_wallet = bool(args.wallet)
    if not include_binance and not include_bybit and not include_wallet:
        raise SystemExit("At least one venue required (default: Binance + Bybit).")

    tracker = _build_tracker(
        include_binance=include_binance,
        include_bybit=include_bybit,
        include_wallet=include_wallet,
    )
    rpc = _rpc(args.rpc)

    try:
        asyncio.run(_run_live(tracker, rpc, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
