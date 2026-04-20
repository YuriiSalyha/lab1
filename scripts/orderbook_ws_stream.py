#!/usr/bin/env python3
"""
Stream a normalized L2 order book over WebSocket (REST snapshot + deltas).

Example::

    python scripts/orderbook_ws_stream.py ETH/USDT --exchange binance
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

# Defaults (overridable via CLI)
PRINT_EVERY_N_UPDATES = 1


async def _main_async(args: argparse.Namespace) -> None:
    from config.config import BINANCE_CONFIG, BYBIT_CONFIG
    from exchange.client import ExchangeClient
    from exchange.orderbook_ws_runner import OrderBookWsRunner

    load_dotenv()
    cfg = BYBIT_CONFIG if args.exchange == "bybit" else BINANCE_CONFIG
    client = ExchangeClient(cfg, exchange_id=args.exchange)
    counter = 0

    async def on_book(d: dict) -> None:
        nonlocal counter
        counter += 1
        if counter % PRINT_EVERY_N_UPDATES != 0:
            return
        bb = d.get("best_bid")
        ba = d.get("best_ask")
        mid = d.get("mid_price")
        print(
            f"ts={d.get('timestamp')} mid={mid} bid={bb} ask={ba} "
            f"spread_bps={d.get('spread_bps')}",
            flush=True,
        )

    runner = OrderBookWsRunner(client, args.symbol, on_book, rest_limit=args.depth)
    await runner.run_forever()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="WebSocket order book stream (Binance / Bybit)")
    p.add_argument("symbol", help="Unified pair e.g. ETH/USDT")
    p.add_argument(
        "--exchange",
        default="binance",
        choices=("binance", "bybit"),
        help="ccxt exchange id",
    )
    p.add_argument("--depth", type=int, default=100, help="REST snapshot depth levels")
    args = p.parse_args(argv)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
