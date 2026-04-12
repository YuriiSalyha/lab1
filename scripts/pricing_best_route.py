#!/usr/bin/env python3
"""Find the best multi-hop route between two ERC-20 tokens over given Uniswap V2 pools.

Standalone script: no imports from other files under ``scripts/`` (safe to run alone).

Usage:

    python scripts/pricing_best_route.py \\
        --token-in 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 \\
        --token-out 0xdAC17F958D2ee523a2206206994597C13D831ec7 \\
        --amount 10000

Optional ``--pools`` defaults to several liquid mainnet Uniswap V2 pairs
(WETH hub to USDC, USDT, DAI, plus WBTC/WETH).

``--discover fetch`` loads top pairs from a Uniswap V2 subgraph (see ``UNISWAP_V2_SUBGRAPH_URL``
or ``THEGRAPH_API_KEY``) and merges them with ``--pools``; ``--discover cache`` reads a JSON cache.

``--v3-resolve T0,T1`` queries the canonical V3 factory for default fee tiers and adds those pools
(alongside repeatable ``--v3-pool ADDR``).

Requires: MAINNET_RPC / RPC_ENDPOINT / ETH_MAINNET_RPC or ``--rpc``
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient
from core.types import Address, Token, TokenAmount
from pricing.liquidity_pool import LiquidityPoolQuote, as_liquidity_quote
from pricing.route_finder import RouteFinder
from pricing.uniswap_v2_discovery import (
    fetch_pair_rows_paginated,
    load_pair_cache,
    merge_discovered_with_explicit,
    resolve_subgraph_url,
    rows_to_pairs,
    save_pair_cache,
)
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_discovery import pools_for_pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter

POOL_WETH_USDC = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")
POOL_WETH_USDT = Address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852")
POOL_WETH_DAI = Address("0xA478c2975Ab1Ea89e8196811F51A7B7Ade33eB11")
# Mainnet Uniswap V2 WBTC/WETH (factory getPair; not the obsolete 0xBb2b...163019... address).
POOL_WBTC_WETH = Address("0xBb2b8038a1640196FbE3e38816F3e67Cba72D940")
_DEFAULT_POOL_ADDRS = (
    POOL_WETH_USDC,
    POOL_WETH_USDT,
    POOL_WETH_DAI,
    POOL_WBTC_WETH,
)
_DEFAULT_POOLS_STR = ",".join(a.checksum for a in _DEFAULT_POOL_ADDRS)


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


def _token_from_address(client: ChainClient, addr: Address) -> Token:
    meta = client.token_cache.get(addr.checksum)
    sym = str(meta.get("symbol") or "UNKNOWN")
    dec = int(meta.get("decimals") or 18)
    return Token(address=addr, symbol=sym, decimals=dec)


def _parse_amount_human(s: str) -> Decimal:
    raw = s.strip().replace("_", "")
    if not raw:
        raise SystemExit("--amount must be non-empty")
    try:
        return Decimal(raw)
    except InvalidOperation as e:
        raise SystemExit(f"Invalid --amount (need a decimal number): {s!r}") from e


def _parse_pool_addresses(pools_csv: str) -> list[Address]:
    parts = [p.strip() for p in pools_csv.split(",") if p.strip()]
    if not parts:
        raise SystemExit("--pools must list at least one pair address")
    return [Address(p) for p in parts]


def _pair_from_chain_mainnet(addr: Address, client: ChainClient) -> UniswapV2Pair:
    """Load a V2 pair; exit with a clear hint if RPC is not Ethereum mainnet."""
    try:
        return UniswapV2Pair.from_chain(addr, client)
    except Exception as e:
        raise SystemExit(
            f"Failed to read Uniswap V2 pair at {addr.checksum}: {e}\n"
            "Default --pools are deployed on Ethereum mainnet (chain id 1). "
            "Point RPC_ENDPOINT, MAINNET_RPC, or ETH_MAINNET_RPC at a mainnet HTTP URL, "
            "or pass --rpc https://... (not Sepolia or other networks)."
        ) from e


def _load_pools_from_csv(client: ChainClient, pools_csv: str) -> list[UniswapV2Pair]:
    return [_pair_from_chain_mainnet(a, client) for a in _parse_pool_addresses(pools_csv)]


def _build_pool_list(
    client: ChainClient,
    *,
    pools_csv: str,
    discover: str,
    pair_cache: Path,
    subgraph_url_cli: str | None,
    min_reserve_usd: str,
    max_pools: int,
) -> list[UniswapV2Pair]:
    explicit_addrs = _parse_pool_addresses(pools_csv)
    if discover == "off":
        return _load_pools_from_csv(client, pools_csv)

    if discover == "fetch":
        try:
            subgraph_url = resolve_subgraph_url(subgraph_url_cli)
        except ValueError as e:
            raise SystemExit(str(e)) from e
        rows = fetch_pair_rows_paginated(
            subgraph_url,
            min_reserve_usd=min_reserve_usd,
            max_pairs=max_pools,
        )
        save_pair_cache(
            pair_cache,
            subgraph_url=subgraph_url,
            min_reserve_usd=min_reserve_usd,
            pair_rows=rows,
        )
        discovered = rows_to_pairs(rows)
    else:
        try:
            rows, _meta = load_pair_cache(pair_cache)
        except (FileNotFoundError, ValueError) as e:
            raise SystemExit(f"{e} Use --discover fetch first, or fix --pair-cache path.") from e
        discovered = rows_to_pairs(rows)

    return merge_discovered_with_explicit(
        discovered,
        explicit_addrs,
        client,
        load_pair=_pair_from_chain_mainnet,
    )


def _tokens_in_pools(pools: list[LiquidityPoolQuote | UniswapV2Pair]) -> set[Token]:
    out: set[Token] = set()
    for pool in pools:
        q = as_liquidity_quote(pool)
        out.add(q.token0)
        out.add(q.token1)
    return out


def _merge_v3_pools(
    client: ChainClient,
    base_pools: list[UniswapV2Pair],
    v3_pool_addrs: list[Address],
) -> list[LiquidityPoolQuote | UniswapV2Pair]:
    out: list[LiquidityPoolQuote | UniswapV2Pair] = list(base_pools)
    for addr in v3_pool_addrs:
        out.append(UniswapV3PoolQuoter.from_chain(addr, client))
    return out


def _report_no_route(
    pools: list[LiquidityPoolQuote | UniswapV2Pair],
    token_in: Token,
    token_out: Token,
    addr_in: Address,
    addr_out: Address,
) -> None:
    covered = _tokens_in_pools(pools)
    symbols = sorted({t.symbol for t in covered})
    print("No route found using the loaded pools (V2 and any --v3-pool).")
    print(f"Tokens present in loaded pools: {', '.join(symbols)}")
    if token_in not in covered:
        print(
            f"token-in {token_in.symbol} ({addr_in.checksum}) is not in any listed pair; "
            "add a V2 pair that includes it (often WETH/token-in on mainnet)."
        )
    elif token_out not in covered:
        print(
            f"token-out {token_out.symbol} ({addr_out.checksum}) is not in any listed pair; "
            "extend --pools with a pair that lists this token."
        )
    else:
        print(
            "Both tokens appear in the pool set but no path connects them within the hop limit; "
            "add more pair addresses."
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Best route by net output over Uniswap V2 (+ optional V3) pools",
    )
    p.add_argument("--rpc", default=None, help="HTTP mainnet RPC (overrides env)")
    p.add_argument(
        "--token-in",
        required=True,
        metavar="ADDR",
        help="ERC-20 you sell (checksummed 0x address)",
    )
    p.add_argument(
        "--token-out",
        required=True,
        metavar="ADDR",
        help="ERC-20 you buy (checksummed 0x address)",
    )
    p.add_argument(
        "--amount",
        required=True,
        metavar="HUMAN",
        help="Human amount of token-in (uses token-in decimals from chain)",
    )
    p.add_argument(
        "--pools",
        default=_DEFAULT_POOLS_STR,
        help=(
            "Comma-separated Uniswap V2 pair addresses; merged with --discover and "
            "refreshed on-chain (default: WETH/USDC, WETH/USDT, WETH/DAI, WBTC/WETH)"
        ),
    )
    p.add_argument(
        "--discover",
        choices=("off", "fetch", "cache"),
        default="off",
        help=(
            "off: use only --pools. fetch: query subgraph, write --pair-cache, merge. "
            "cache: load --pair-cache only (no subgraph HTTP)"
        ),
    )
    p.add_argument(
        "--pair-cache",
        type=Path,
        default=Path(".cache") / "uniswap_v2_pairs.json",
        help="JSON cache path for --discover fetch|cache",
    )
    p.add_argument(
        "--subgraph-url",
        default=None,
        help="Override Uniswap V2 subgraph URL (else env / THEGRAPH_API_KEY)",
    )
    p.add_argument(
        "--max-pools",
        type=int,
        default=2000,
        help="Max pairs to load from subgraph when --discover fetch",
    )
    p.add_argument(
        "--min-reserve-usd",
        default="10000",
        help="Subgraph filter reserveUSD_gt (string BigDecimal, e.g. 10000)",
    )
    p.add_argument(
        "--max-hops",
        type=int,
        default=3,
        help="Maximum pools per route",
    )
    p.add_argument(
        "--gas-gwei",
        type=int,
        default=30,
        dest="gas_gwei",
        help="Gas price for net-output math",
    )
    p.add_argument(
        "--v3-pool",
        action="append",
        default=[],
        metavar="ADDR",
        help=(
            "Uniswap V3 pool address (repeatable); fee/token0/token1 read on-chain; "
            "quotes via QuoterV2 eth_call"
        ),
    )
    p.add_argument(
        "--v3-resolve",
        action="append",
        default=[],
        metavar="T0,T1",
        help=(
            "Two ERC-20 addresses (comma-separated); add deployed V3 pools for 500/3000/10000 fee "
            "(mainnet factory)"
        ),
    )
    args = p.parse_args()

    addr_in = Address(args.token_in)
    addr_out = Address(args.token_out)
    if addr_in.lower == addr_out.lower:
        raise SystemExit("token-in and token-out must differ")

    rpc = _http_rpc(args.rpc)
    client = ChainClient([rpc])

    token_in = _token_from_address(client, addr_in)
    token_out = _token_from_address(client, addr_out)

    amt_human = _parse_amount_human(args.amount)
    amount_in = int(
        TokenAmount.from_human(
            amt_human,
            decimals=token_in.decimals,
            symbol=token_in.symbol,
        ).raw
    )
    if amount_in <= 0:
        raise SystemExit("--amount must be positive")

    if args.max_hops < 1:
        raise SystemExit("--max-hops must be at least 1")
    if args.max_pools < 1:
        raise SystemExit("--max-pools must be at least 1")

    pools = _build_pool_list(
        client,
        pools_csv=args.pools,
        discover=args.discover,
        pair_cache=args.pair_cache,
        subgraph_url_cli=args.subgraph_url,
        min_reserve_usd=args.min_reserve_usd,
        max_pools=args.max_pools,
    )
    v3_seen: set[str] = set()
    v3_addrs: list[Address] = []
    for raw in args.v3_pool:
        a = Address(raw)
        if a.lower not in v3_seen:
            v3_seen.add(a.lower)
            v3_addrs.append(a)
    for pair_spec in args.v3_resolve:
        toks = [x.strip() for x in pair_spec.split(",") if x.strip()]
        if len(toks) != 2:
            raise SystemExit("--v3-resolve expects TOKEN0,TOKEN1 (comma-separated)")
        for _fee, pool_cs in pools_for_pair(client.w3, toks[0], toks[1]):
            a = Address(pool_cs)
            if a.lower not in v3_seen:
                v3_seen.add(a.lower)
                v3_addrs.append(a)
    if v3_addrs:
        pools = _merge_v3_pools(client, pools, v3_addrs)
    finder = RouteFinder(pools)

    route, net_out = finder.find_best_route(
        token_in,
        token_out,
        amount_in,
        args.gas_gwei,
        max_hops=args.max_hops,
    )
    if route is None:
        _report_no_route(pools, token_in, token_out, addr_in, addr_out)
        return

    gross = route.get_output(amount_in)
    print(
        f"From {token_in.symbol} ({addr_in.checksum}) -> "
        f"{token_out.symbol} ({addr_out.checksum})"
    )
    print(f"amount_in={amount_in} raw ({amt_human} {token_in.symbol} human)")
    print(f"Gas price: {args.gas_gwei} gwei")
    print(f"Best path: {' -> '.join(t.symbol for t in route.path)}  ({route.num_hops} hop(s))")
    print(f"Gross output ({token_out.symbol} raw): {gross}")
    print(f"Net output (after gas in {token_out.symbol}): {net_out}")
    print()
    print("All routes (by net):")
    for row in finder.compare_routes(
        token_in,
        token_out,
        amount_in,
        args.gas_gwei,
        max_hops=args.max_hops,
    ):
        path = " -> ".join(t.symbol for t in row["route"].path)
        print(
            f"  net={row['net_output']} gross={row['gross_output']} "
            f"gas_out={row['gas_cost']} {path}"
        )


if __name__ == "__main__":
    main()
