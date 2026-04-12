#!/usr/bin/env python3
"""Scan loaded V2 pools for simple cyclic arbitrage (discrete amount grid)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient
from core.types import Address
from pricing.arbitrage_scanner import ArbitrageScanner, default_amount_grid
from pricing.batch_quote import BatchQuoteExecutor
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter


def _rpc(cli: str | None) -> str:
    load_dotenv()
    if cli and cli.strip():
        return cli.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise SystemExit("Set MAINNET_RPC / ETH_MAINNET_RPC / RPC_ENDPOINT or --rpc")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Scan Uniswap V2/V3 pools for simple cyclic arb (discrete amount grid)",
    )
    p.add_argument("--rpc", default=None)
    p.add_argument(
        "--pools",
        default="",
        help="Comma-separated Uniswap V2 pair addresses (optional if --v3-pool given)",
    )
    p.add_argument(
        "--v3-pool",
        action="append",
        default=[],
        metavar="ADDR",
        help="Repeatable: V3 pool address (quotes via QuoterV2; use with --batch for Multicall)",
    )
    p.add_argument(
        "--batch",
        action="store_true",
        help="Batch V3 quotes via Multicall3 (recommended with --v3-pool)",
    )
    p.add_argument("--max-cycle-len", type=int, default=3, help="Max pools per cycle (2–4)")
    p.add_argument("--gas-gwei", type=int, default=30)
    p.add_argument(
        "--max-raw",
        type=int,
        default=10**18,
        help="Upper bound for amount grid (raw units)",
    )
    p.add_argument(
        "--grid-steps",
        type=int,
        default=16,
        help="Max powers-of-two steps in default grid",
    )
    args = p.parse_args()

    if args.max_cycle_len < 2 or args.max_cycle_len > 4:
        raise SystemExit("--max-cycle-len must be 2–4")

    parts = [x.strip() for x in args.pools.split(",") if x.strip()]
    v3_addrs = [x.strip() for x in args.v3_pool if x.strip()]
    if not parts and not v3_addrs:
        raise SystemExit("Provide --pools and/or one or more --v3-pool")

    client = ChainClient([_rpc(args.rpc)])
    pools: list = [UniswapV2Pair.from_chain(Address(x), client) for x in parts]
    for a in v3_addrs:
        pools.append(UniswapV3PoolQuoter.from_chain(Address(a), client))

    batch_ex: BatchQuoteExecutor | None = None
    if args.batch:
        batch_ex = BatchQuoteExecutor(w3=client.w3)
    elif v3_addrs:
        print(
            "Note: V3 pools perform one eth_call per quote per hop; "
            "pass --batch to use Multicall3.",
            file=sys.stderr,
        )

    scanner = ArbitrageScanner(
        pools,
        max_cycle_len=args.max_cycle_len,
        gas_price_gwei=args.gas_gwei,
        batch_executor=batch_ex,
    )
    grid = default_amount_grid(max_raw=args.max_raw, steps=args.grid_steps)
    opps = scanner.find_opportunities(amount_candidates=grid)

    if not opps:
        print("No raw-profit opportunities on the grid (try more pools or larger --max-raw).")
        return

    print(f"Found {len(opps)} candidate(s) (sorted by profit_net):\n")
    for i, o in enumerate(opps[:50], 1):
        syms = " -> ".join(t.symbol for t in o.path)
        print(
            f"{i}. path={syms} amount_in={o.amount_in} amount_out={o.amount_out} "
            f"profit_raw={o.profit_raw} profit_bps={o.profit_bps} "
            f"gas_est={o.gas_estimate} gas_in_start≈{o.gas_cost_start_token} "
            f"profit_net={o.profit_net}"
        )


if __name__ == "__main__":
    main()
