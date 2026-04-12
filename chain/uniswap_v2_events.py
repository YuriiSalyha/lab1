"""Uniswap V2 pair event topics and log filters (``eth_getLogs`` / WS ``logs`` subscribe)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eth_utils import to_checksum_address

# keccak256("Sync(uint112,uint112)")
UNISWAP_V2_SYNC_TOPIC0 = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"


def sync_logs_filter(pair_address: str) -> dict:
    """
    Filter dict for V2 pair ``Sync`` events only.

    Use with ``eth_getLogs`` (add ``fromBlock`` / ``toBlock``) or
    ``eth_subscribe("logs", filter)``.
    """
    return {
        "address": to_checksum_address(pair_address),
        "topics": [UNISWAP_V2_SYNC_TOPIC0],
    }


def reserves_from_sync_parse_result(parsed: Mapping[str, Any]) -> tuple[int, int] | None:
    """
    If *parsed* is a :meth:`chain.decoder.TransactionDecoder.parse_event` result for
    ``Sync``, return ``(reserve0, reserve1)``; otherwise ``None``.
    """
    if parsed.get("name") != "Sync":
        return None
    dec = parsed.get("decoded")
    if not isinstance(dec, Mapping):
        return None
    r0, r1 = dec.get("reserve0"), dec.get("reserve1")
    if r0 is None or r1 is None:
        return None
    i0, i1 = int(r0), int(r1)
    if i0 <= 0 or i1 <= 0:
        return None
    return i0, i1
