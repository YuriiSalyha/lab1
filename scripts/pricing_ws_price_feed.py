#!/usr/bin/env python3
"""Stream Uniswap V2 pair reserves from mainnet over WebSocket (``Sync`` logs).

Usage:

    python scripts/pricing_ws_price_feed.py --pool 0x... [--rpc URL] [--ws wss://...]
    python scripts/pricing_ws_price_feed.py --pool 0x... --token WETH --impact-sizes 1e18,1e19

Requires: HTTP RPC for initial pair metadata; WebSocket for ``logs`` (env or ``--ws``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient  # noqa: E402
from chain.ws_env import resolve_websocket_url  # noqa: E402
from core.types import Address  # noqa: E402
from pricing.uniswap_v2_pair import UniswapV2Pair  # noqa: E402
from pricing.v2_pool_price_feed import V2PoolPriceFeed, V2PoolPriceTick  # noqa: E402


def _http_rpc(cli_rpc: str | None) -> str:
    load_dotenv()
    if cli_rpc and cli_rpc.strip():
        return cli_rpc.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise SystemExit("Set MAINNET_RPC, ETH_MAINNET_RPC, or RPC_ENDPOINT, or pass --rpc")


def _token_for_symbol(pair: UniswapV2Pair, symbol: str):
    raw = symbol.strip()
    if not raw:
        raise SystemExit("--token must be non-empty when using --impact-sizes")
    key = raw.upper()
    for t in (pair.token0, pair.token1):
        if t.symbol.upper() == key:
            return t
    if key == "ETH":
        for t in (pair.token0, pair.token1):
            if t.symbol.upper() == "WETH":
                return t
    opts = f"{pair.token0.symbol}, {pair.token1.symbol}"
    raise SystemExit(f"Token symbol {raw!r} is not on this pair. Use one of: {opts}")


def _parse_impact_sizes(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip().replace("_", "")
        if not part:
            continue
        try:
            out.append(int(Decimal(part)))
        except Exception as e:
            raise SystemExit(f"Invalid impact size {part!r} (use integer raw units)") from e
    return out


def _tick_json(tick: V2PoolPriceTick) -> dict:
    d: dict = {
        "block_number": tick.block_number,
        "log_index": tick.log_index,
        "reserve0": tick.reserve0,
        "reserve1": tick.reserve1,
        "spot_price_token0": str(tick.spot_price_token0),
        "spot_price_token1": str(tick.spot_price_token1),
    }
    if tick.impact_pct_by_amount:
        d["impact_pct_by_amount"] = {str(k): str(v) for k, v in tick.impact_pct_by_amount.items()}
    return d


def main() -> None:
    p = argparse.ArgumentParser(description="WS stream: Uniswap V2 pair Sync → reserves / spot")
    p.add_argument("--rpc", default=None, help="HTTP RPC for loading pair metadata")
    p.add_argument("--ws", default=None, help="WebSocket URL (overrides MAINNET_WS / WS_URL / …)")
    p.add_argument("--pool", required=True, help="Uniswap V2 pair address")
    p.add_argument(
        "--token",
        default=None,
        metavar="SYMBOL",
        help="Sell token symbol for optional --impact-sizes (e.g. WETH)",
    )
    p.add_argument(
        "--impact-sizes",
        default=None,
        metavar="RAW_LIST",
        help="Comma-separated raw amount_in values for price_impact_pct on each tick",
    )
    p.add_argument(
        "--output-format",
        choices=("jsonl", "csv"),
        default="jsonl",
        dest="output_format",
        help="Output format (default jsonl)",
    )
    args = p.parse_args()

    rpc = _http_rpc(args.rpc)
    try:
        ws = resolve_websocket_url(args.ws)
    except ValueError as e:
        raise SystemExit(str(e)) from e

    client = ChainClient([rpc])
    pair = UniswapV2Pair.from_chain(Address(args.pool), client)

    impact_token = None
    impact_amounts: list[int] | None = None
    if args.impact_sizes:
        if not args.token:
            raise SystemExit("--token is required when using --impact-sizes")
        impact_token = _token_for_symbol(pair, args.token)
        impact_amounts = _parse_impact_sizes(args.impact_sizes)
        if not impact_amounts:
            raise SystemExit("--impact-sizes must list at least one positive integer")

    if args.output_format == "csv":
        hdr = "block,log_index,r0,r1,spot0,spot1"
        if impact_amounts:
            for a in impact_amounts:
                hdr += f",impact_pct_{a}"
        print(hdr)

        def on_tick_csv(tick: V2PoolPriceTick) -> None:
            row = [
                str(tick.block_number),
                str(tick.log_index),
                str(tick.reserve0),
                str(tick.reserve1),
                str(tick.spot_price_token0),
                str(tick.spot_price_token1),
            ]
            if impact_amounts and tick.impact_pct_by_amount:
                for a in impact_amounts:
                    row.append(str(tick.impact_pct_by_amount.get(a, "")))
            print(",".join(row))

        feed = V2PoolPriceFeed(
            ws,
            pair,
            on_tick_csv,
            impact_token=impact_token,
            impact_amounts=impact_amounts,
        )
        print("Streaming (CSV). Ctrl+C to stop.\n", file=sys.stderr)
        asyncio.run(feed.run_forever())
        return

    def on_tick_jsonl(tick: V2PoolPriceTick) -> None:
        print(json.dumps(_tick_json(tick), sort_keys=True))

    feed = V2PoolPriceFeed(
        ws, pair, on_tick_jsonl, impact_token=impact_token, impact_amounts=impact_amounts
    )
    print("Streaming (JSONL). Ctrl+C to stop.\n", file=sys.stderr)
    asyncio.run(feed.run_forever())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
