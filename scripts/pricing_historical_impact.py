#!/usr/bin/env python3
"""Historical price impact from Uniswap V2 ``Sync`` logs (HTTP ``eth_getLogs``).

Usage:

    python scripts/pricing_historical_impact.py \\
        --pool 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc \\
        --from-block 18000000 --to-block 18000100 \\
        --token WETH --sizes 1e18,1e19

**Archive** access is required for old ``from_block`` values.

Default ``--chunk-blocks 10`` matches Alchemy free-tier ``eth_getLogs`` limits;
increase on paid RPC.

Requires: MAINNET_RPC / RPC_ENDPOINT / ETH_MAINNET_RPC or ``--rpc``
"""

from __future__ import annotations

import argparse
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
from core.errors import InvalidAddressError  # noqa: E402
from core.types import Address  # noqa: E402
from pricing.historical_price_impact import (  # noqa: E402
    fetch_sync_snapshots,
    series_impact_for_sizes,
)
from pricing.uniswap_v2_pair import UniswapV2Pair  # noqa: E402


def _http_rpc(cli_rpc: str | None) -> str:
    load_dotenv()
    if cli_rpc and cli_rpc.strip():
        return cli_rpc.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise SystemExit("Set MAINNET_RPC, ETH_MAINNET_RPC, or RPC_ENDPOINT, or pass --rpc")


def _pair_address(cli_value: str) -> Address:
    raw = cli_value.strip()
    if raw in ("...", ".", "") or not raw.startswith("0x"):
        raise SystemExit(
            "Invalid --pool: use the real 0x pair address (not the literal ...).\n"
            "Example WETH/USDC: 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"
        )
    try:
        return Address(raw)
    except InvalidAddressError as e:
        raise SystemExit(f"Invalid --pool {raw!r}: must be a 40-hex-digit Ethereum address.") from e


def _token_for_symbol(pair: UniswapV2Pair, symbol: str):
    raw = symbol.strip()
    if not raw:
        raise SystemExit("--token must be non-empty")
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


def _parse_sizes(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip().replace("_", "")
        if not part:
            continue
        try:
            out.append(int(Decimal(part)))
        except Exception as e:
            raise SystemExit(f"Invalid size {part!r} (use integer raw units)") from e
    if not out:
        raise SystemExit("--sizes must list at least one positive integer")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Historical V2 price impact from Sync logs")
    p.add_argument("--rpc", default=None, help="HTTP RPC (archive for old blocks)")
    p.add_argument("--pool", required=True, help="Uniswap V2 pair address")
    p.add_argument("--from-block", type=int, required=True, dest="from_block")
    p.add_argument("--to-block", type=int, required=True, dest="to_block")
    p.add_argument(
        "--token",
        required=True,
        metavar="SYMBOL",
        help="Token in (sell side), e.g. WETH",
    )
    p.add_argument(
        "--sizes",
        required=True,
        help="Comma-separated raw amount_in values (same units as on-chain)",
    )
    p.add_argument(
        "--output-format",
        choices=("jsonl", "csv"),
        default="jsonl",
        dest="output_format",
    )
    p.add_argument(
        "--chunk-blocks",
        type=int,
        default=10,
        metavar="N",
        help=(
            "Target max blocks per eth_getLogs (default 10). On HTTP 400 the span is "
            "reduced automatically; use a large value on paid nodes to reduce RPC calls"
        ),
    )
    args = p.parse_args()

    rpc = _http_rpc(args.rpc)
    client = ChainClient([rpc])
    pair = UniswapV2Pair.from_chain(_pair_address(args.pool), client)
    token_in = _token_for_symbol(pair, args.token)
    sizes = _parse_sizes(args.sizes)

    snaps = fetch_sync_snapshots(
        client.w3,
        pair.address,
        args.from_block,
        args.to_block,
        chunk_blocks=args.chunk_blocks,
    )
    rows = series_impact_for_sizes(snaps, pair, token_in, sizes)

    if args.output_format == "jsonl":
        for row in rows:
            d = {
                "block_number": row["block_number"],
                "log_index": row["log_index"],
                "reserve0": row["reserve0"],
                "reserve1": row["reserve1"],
                "impact_pct_by_amount": {
                    str(k): str(v) for k, v in row["impact_pct_by_amount"].items()
                },
            }
            print(json.dumps(d, sort_keys=True))
        return

    hdr = ["block", "log_index", "r0", "r1"] + [f"impact_pct_{a}" for a in sizes]
    print(",".join(hdr))
    for row in rows:
        imp = row["impact_pct_by_amount"]
        cells = [
            str(row["block_number"]),
            str(row["log_index"]),
            str(row["reserve0"]),
            str(row["reserve1"]),
        ] + [str(imp[a]) for a in sizes]
        print(",".join(cells))


if __name__ == "__main__":
    main()
