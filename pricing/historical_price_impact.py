"""Historical Uniswap V2 reserves from ``eth_getLogs`` (``Sync``) and price-impact series."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import requests
from web3 import Web3

from chain.decoder import TransactionDecoder
from chain.uniswap_v2_events import reserves_from_sync_parse_result, sync_logs_filter
from core.types import Address, Token
from pricing.price_impact_analyzer import impact_row_for_amount
from pricing.uniswap_v2_pair import UniswapV2Pair

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReserveSnapshot:
    block_number: int
    log_index: int
    reserve0: int
    reserve1: int


def _rpc_uint(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return int(val, 16) if val.startswith("0x") else int(val, 10)
    if isinstance(val, (bytes, bytearray, memoryview)):
        return int.from_bytes(val, "big", signed=False)
    return int(val)


def snapshot_from_log(log: dict[str, Any]) -> ReserveSnapshot | None:
    """Parse one RPC log into a snapshot, or ``None`` if not a valid ``Sync``."""
    parsed = TransactionDecoder.parse_event(log)
    got = reserves_from_sync_parse_result(parsed)
    if got is None:
        return None
    r0, r1 = got
    return ReserveSnapshot(
        block_number=_rpc_uint(log.get("blockNumber")),
        log_index=_rpc_uint(log.get("logIndex")),
        reserve0=r0,
        reserve1=r1,
    )


def fetch_sync_snapshots(
    w3: Web3,
    pair_address: Address,
    from_block: int,
    to_block: int,
    *,
    chunk_blocks: int = 10,
) -> list[ReserveSnapshot]:
    """
    Return all ``Sync`` reserve snapshots for the pair in ``[from_block, to_block]``.

    **Archive RPC** is required when *from_block* is far behind the chain tip.

    *chunk_blocks*: desired max inclusive span per ``eth_getLogs`` call. If the node
    returns **HTTP 400** (common when the range exceeds the provider cap, e.g. Alchemy
    free ≈10 blocks), the span is halved and retried until the call succeeds; the
    working span is then reused for the rest of the range. Pass a large value on paid
    nodes to minimize round-trips.
    """
    if from_block < 0 or to_block < from_block:
        raise ValueError("invalid block range")
    if chunk_blocks < 1:
        raise ValueError("chunk_blocks must be >= 1")

    base = sync_logs_filter(pair_address.checksum)
    out: list[ReserveSnapshot] = []
    start = from_block
    effective_cap: int | None = None

    while start <= to_block:
        remaining = to_block - start + 1
        span = min(chunk_blocks, remaining)
        if effective_cap is not None:
            span = min(span, effective_cap)

        while True:
            end = start + span - 1
            flt: dict[str, Any] = {
                **base,
                "fromBlock": Web3.to_hex(start),
                "toBlock": Web3.to_hex(end),
            }
            try:
                raw_logs = w3.eth.get_logs(flt)
                effective_cap = span
                break
            except requests.exceptions.HTTPError as err:
                resp = getattr(err, "response", None)
                code = getattr(resp, "status_code", None) if resp is not None else None
                if code == 400 and span > 1:
                    new_span = max(1, span // 2)
                    if new_span == span:
                        new_span = span - 1
                    logger.debug(
                        "eth_getLogs HTTP 400 for blocks %s–%s; retrying span %s → %s",
                        start,
                        end,
                        span,
                        new_span,
                    )
                    span = new_span
                    continue
                raise

        for entry in raw_logs:
            log = dict(entry)
            snap = snapshot_from_log(log)
            if snap is not None:
                out.append(snap)
        start = end + 1

    out.sort(key=lambda s: (s.block_number, s.log_index))
    return out


def series_impact_for_sizes(
    snapshots: list[ReserveSnapshot],
    template_pair: UniswapV2Pair,
    token_in: Token,
    amount_ins: list[int],
) -> list[dict[str, Any]]:
    """
    For each snapshot, compute :func:`impact_row_for_amount` per *amount_in*.

    Returns rows:
    ``block_number``, ``log_index``, ``reserve0``, ``reserve1``,
    ``impact_pct_by_amount`` (``amount_in`` → ``price_impact_pct``).
    """
    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        pair = template_pair.with_reserves(snap.reserve0, snap.reserve1)
        by_amt: dict[int, Decimal] = {}
        for amt in amount_ins:
            if amt <= 0:
                raise ValueError(f"amount_in must be positive, got {amt}")
            row = impact_row_for_amount(pair, token_in, amt)
            by_amt[amt] = cast(Decimal, row["price_impact_pct"])
        rows.append(
            {
                "block_number": snap.block_number,
                "log_index": snap.log_index,
                "reserve0": snap.reserve0,
                "reserve1": snap.reserve1,
                "impact_pct_by_amount": by_amt,
            }
        )
    return rows
