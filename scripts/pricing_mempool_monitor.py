#!/usr/bin/env python3
"""Subscribe to pending mainnet txs and print decoded Uniswap V2 router swaps.

Standalone script: no imports from other files under ``scripts/`` (safe to run alone).

Usage:

    python scripts/pricing_mempool_monitor.py
    python scripts/pricing_mempool_monitor.py --ws wss://eth-mainnet.g.alchemy.com/v2/KEY

Requires: MAINNET_WS / WS_URL / ... or --ws (must be wss:// Ethereum mainnet WebSocket)

Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.ws_env import resolve_websocket_url
from pricing.mempool_monitor import MempoolMonitor
from pricing.parsed_swap import ParsedSwap

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)


def _ws_url(cli_ws: str | None) -> str:
    try:
        return resolve_websocket_url(cli_ws)
    except ValueError as e:
        raise SystemExit(str(e)) from e


def _on_swap(swap: ParsedSwap) -> None:
    tin = swap.token_in.checksum if swap.token_in else "?"
    tout = swap.token_out.checksum if swap.token_out else "?"
    print(
        f"[{swap.method}] tx={swap.tx_hash[:20]}... "
        f"{tin} -> {tout} amount_in={swap.amount_in} min_out={swap.min_amount_out}"
    )


async def _run(ws_url: str) -> None:
    mon = MempoolMonitor(ws_url, _on_swap)
    await mon.start()


def main() -> None:
    p = argparse.ArgumentParser(description="Mempool: Uniswap V2 swaps on pending txs (WebSocket)")
    p.add_argument("--ws", default=None, help="Mainnet wss:// URL (overrides env)")
    args = p.parse_args()

    ws = _ws_url(args.ws)
    print("Connecting to WebSocket (showing Uniswap V2 swaps only). Press Ctrl+C to stop.\n")
    try:
        asyncio.run(_run(ws))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
