"""Uniswap V2 pair discovery via The Graph subgraph + optional JSON cache.

Uses the official Uniswap V2 Ethereum subgraph (see Uniswap docs). Resolve the HTTP
endpoint with ``UNISWAP_V2_SUBGRAPH_URL`` (full gateway URL) or ``THEGRAPH_API_KEY``
(embedded into the standard gateway path for subgraph id
``A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDum``).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from chain.client import ChainClient
from core.types import Address
from pricing.uniswap_v2_pair import UniswapV2Pair

DEFAULT_USER_AGENT = "lab1-pricing/0.1"
_UNISWAP_V2_ETHEREUM_SUBGRAPH_ID = "A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDum"

PAIRS_PAGE_QUERY = """
query PairsPage($first: Int!, $skip: Int!, $minUsd: BigDecimal!) {
  pairs(
    first: $first
    skip: $skip
    orderBy: reserveUSD
    orderDirection: desc
    where: { reserveUSD_gt: $minUsd }
  ) {
    id
    reserve0
    reserve1
    token0 { id symbol decimals }
    token1 { id symbol decimals }
  }
}
"""

PostJsonFn = Callable[[str, dict[str, Any], int], dict[str, Any]]


def _default_post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Subgraph HTTP {e.code}: {body[:500]}") from e


def resolve_subgraph_url(cli_url: str | None) -> str:
    """Full subgraph HTTP URL (POST target)."""
    if cli_url and cli_url.strip():
        return cli_url.strip()
    env_url = os.environ.get("UNISWAP_V2_SUBGRAPH_URL", "").strip()
    if env_url:
        return env_url
    key = os.environ.get("THEGRAPH_API_KEY", "").strip()
    if key:
        return (
            f"https://gateway.thegraph.com/api/{key}/subgraphs/id/"
            f"{_UNISWAP_V2_ETHEREUM_SUBGRAPH_ID}"
        )
    raise ValueError(
        "Set UNISWAP_V2_SUBGRAPH_URL to your full The Graph gateway URL, "
        "or set THEGRAPH_API_KEY for the default Uniswap V2 Ethereum subgraph."
    )


def fetch_pair_rows_paginated(
    subgraph_url: str,
    *,
    min_reserve_usd: str,
    max_pairs: int,
    page_size: int = 500,
    timeout: int = 120,
    post_json: PostJsonFn | None = None,
) -> list[dict[str, Any]]:
    """Return raw ``pairs`` documents from the subgraph (newest / highest reserveUSD first)."""
    poster = post_json or _default_post_json
    out: list[dict[str, Any]] = []
    skip = 0
    while len(out) < max_pairs:
        first = min(page_size, max_pairs - len(out))
        payload = {
            "query": PAIRS_PAGE_QUERY,
            "variables": {
                "first": first,
                "skip": skip,
                "minUsd": min_reserve_usd,
            },
        }
        data = poster(subgraph_url, payload, timeout)
        if "errors" in data and data["errors"]:
            msgs = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise RuntimeError(f"Subgraph GraphQL errors: {msgs}")
        pairs = (data.get("data") or {}).get("pairs") or []
        if not pairs:
            break
        out.extend(pairs)
        skip += len(pairs)
        if len(pairs) < first:
            break
    return out[:max_pairs]


def rows_to_pairs(rows: Iterable[dict[str, Any]]) -> list[UniswapV2Pair]:
    pairs: list[UniswapV2Pair] = []
    for row in rows:
        p = UniswapV2Pair.from_subgraph_row(row)
        if p is not None:
            pairs.append(p)
    return pairs


def save_pair_cache(
    path: Path,
    *,
    subgraph_url: str,
    min_reserve_usd: str,
    pair_rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "subgraph_url": subgraph_url,
        "min_reserve_usd": min_reserve_usd,
        "fetched_at": int(time.time()),
        "pair_rows": pair_rows,
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def load_pair_cache(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Pair cache not found: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("pair_rows")
    if not isinstance(rows, list):
        raise ValueError("Invalid cache: 'pair_rows' must be a list")
    return rows, doc


def merge_discovered_with_explicit(
    discovered: list[UniswapV2Pair],
    explicit_addresses: list[Address],
    client: ChainClient,
    *,
    load_pair: Callable[[Address, ChainClient], UniswapV2Pair] | None = None,
) -> list[UniswapV2Pair]:
    """Union by pair address; explicit pair loads overwrite subgraph snapshots."""
    loader = load_pair or UniswapV2Pair.from_chain
    by_lower: dict[str, UniswapV2Pair] = {}
    for p in discovered:
        by_lower[p.address.lower] = p
    for addr in explicit_addresses:
        by_lower[addr.lower] = loader(addr, client)
    return list(by_lower.values())
