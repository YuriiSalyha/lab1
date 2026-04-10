#!/usr/bin/env python3
"""Print a price impact table for a Uniswap V2 pool (live reserves).

Standalone script: no imports from other files under ``scripts/`` (safe to run alone).

Usage (from repo root, with venv activated):

    python scripts/pricing_impact_table.py --pool <pair> --token WETH
    python scripts/pricing_impact_table.py --rpc URL --pool <pair> --token USDC

``--pool`` and ``--token`` (ticker you sell, e.g. ``USDC`` / ``WETH``) are required.
Matching is case-insensitive; ``ETH`` is accepted if the pool has ``WETH``. The printed
impact column is the absolute adverse percent vs marginal spot.

Requires: MAINNET_RPC / RPC_ENDPOINT / ETH_MAINNET_RPC or ``--rpc``
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

# Allow `python scripts/...` without PYTHONPATH when not using editable install
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient
from core.types import Address, Token, TokenAmount
from pricing.price_impact_analyzer import PriceImpactAnalyzer
from pricing.uniswap_v2_pair import UniswapV2Pair


def _http_rpc(cli_rpc: str | None) -> str:
    """This script only: resolve mainnet HTTP RPC (env or --rpc)."""
    load_dotenv()
    if cli_rpc and cli_rpc.strip():
        return cli_rpc.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise SystemExit("Set MAINNET_RPC, ETH_MAINNET_RPC, or RPC_ENDPOINT, or pass --rpc")


def _token_in_for_symbol(pair: UniswapV2Pair, symbol: str) -> Token:
    """Resolve sell-side token by symbol against this pair's token0 / token1."""
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


def _parse_size_human(fragment: str) -> Decimal:
    """Parse a --sizes token from the CLI string into Decimal (no float)."""
    s = fragment.strip().replace("_", "")
    if not s:
        raise SystemExit("Empty size in --sizes")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise SystemExit(f"Invalid size (not a decimal): {fragment!r}") from e


def main() -> None:
    p = argparse.ArgumentParser(description="Uniswap V2 price impact table (live reserves)")
    p.add_argument("--rpc", default=None, help="HTTP mainnet RPC (overrides env)")
    p.add_argument(
        "--pool",
        required=True,
        help="Uniswap V2 pair contract address (0x...)",
    )
    p.add_argument(
        "--token",
        required=True,
        metavar="SYMBOL",
        help="Ticker of the token you sell (must match this pool's token0 or token1, e.g. USDC)",
    )
    p.add_argument(
        "--sizes",
        default="0.001,1,100,10000,10_000_000",
        help="Comma-separated human amounts for the input token (e.g. WETH as ETH units)",
    )
    args = p.parse_args()

    rpc = _http_rpc(args.rpc)
    client = ChainClient([rpc])
    pair = UniswapV2Pair.from_chain(Address(args.pool), client)
    token_in: Token = _token_in_for_symbol(pair, args.token)

    raw_sizes: list[int] = []
    for part in args.sizes.split(","):
        part = part.strip()
        if not part:
            continue
        human_amt = _parse_size_human(part)
        ta = TokenAmount.from_human(
            human_amt,
            decimals=token_in.decimals,
            symbol=token_in.symbol,
        )
        raw_sizes.append(int(ta.raw))
    if not raw_sizes:
        raise SystemExit("No sizes parsed from --sizes")

    analyzer = PriceImpactAnalyzer(pair)
    rows = analyzer.generate_impact_table(token_in, raw_sizes)

    print(f"Pool {args.pool}")
    print(f"token0={pair.token0.symbol} token1={pair.token1.symbol}")
    print(f"Input token: {token_in.symbol} ({token_in.decimals} decimals)")
    print("|impact| % = absolute percent deviation from marginal spot (adverse).")
    print()
    hdr = f"{'amount_in (human)':>18} {'amount_out':>22} {'|impact| %':>12} {'exec/spot':>14}"
    print(hdr)
    print("-" * len(hdr))
    zero = Decimal(0)
    for r in rows:
        human_in = TokenAmount(raw=r["amount_in"], decimals=token_in.decimals).human
        spot = r["spot_price"]
        ratio: Decimal = r["execution_price"] / spot if spot != 0 else zero
        impact_abs: Decimal = abs(r["price_impact_pct"])
        impact_s = format(impact_abs, ">11.4f")
        ratio_s = format(ratio, ">14.6f")
        human_s = format(human_in, ">18")
        print(f"{human_s} {r['amount_out']:>22} {impact_s}% {ratio_s}")


if __name__ == "__main__":
    main()
