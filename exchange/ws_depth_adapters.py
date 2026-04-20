"""
Parse exchange WebSocket JSON into snapshot/delta events (no network I/O).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

EventKind = Literal["snapshot", "delta", "ignore"]


@dataclass(frozen=True)
class DepthEvent:
    kind: EventKind
    bids: list[tuple[str, str]]
    asks: list[tuple[str, str]]
    u_first: int | None = None
    u_final: int | None = None
    seq: int | None = None


def _unwrap_binance_payload(msg: dict[str, Any]) -> dict[str, Any]:
    if "data" in msg and isinstance(msg["data"], dict):
        return msg["data"]
    return msg


def parse_binance_depth_json(text: str) -> DepthEvent | None:
    """
    Binance Spot ``depthUpdate`` (wrapped or bare).

    https://binance-docs.github.io/apidocs/spot/en/#diff-depth-stream
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = _unwrap_binance_payload(obj)
    if payload.get("e") != "depthUpdate":
        return None
    bids_raw = payload.get("b") or []
    asks_raw = payload.get("a") or []
    bids: list[tuple[str, str]] = []
    asks: list[tuple[str, str]] = []
    for row in bids_raw:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            bids.append((str(row[0]), str(row[1])))
    for row in asks_raw:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            asks.append((str(row[0]), str(row[1])))
    u_first = payload.get("U")
    u_final = payload.get("u")
    try:
        uf = int(u_first) if u_first is not None else None
    except (TypeError, ValueError):
        uf = None
    try:
        uz = int(u_final) if u_final is not None else None
    except (TypeError, ValueError):
        uz = None
    return DepthEvent(kind="delta", bids=bids, asks=asks, u_first=uf, u_final=uz, seq=uz)


def parse_bybit_orderbook_json(text: str) -> DepthEvent | None:
    """
    Bybit v5 spot public orderbook (snapshot / delta).

    https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    topic = str(obj.get("topic") or "")
    if "orderbook" not in topic:
        return None
    typ = str(obj.get("type") or "").lower()
    data = obj.get("data")
    if not isinstance(data, dict):
        return None
    bids_raw = data.get("b") or []
    asks_raw = data.get("a") or []
    bids: list[tuple[str, str]] = []
    asks: list[tuple[str, str]] = []
    for row in bids_raw:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            bids.append((str(row[0]), str(row[1])))
    for row in asks_raw:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            asks.append((str(row[0]), str(row[1])))
    seq_val = data.get("seq")
    try:
        seq = int(seq_val) if seq_val is not None else None
    except (TypeError, ValueError):
        seq = None
    u_val = data.get("u")
    try:
        uu = int(u_val) if u_val is not None else None
    except (TypeError, ValueError):
        uu = None
    kind: EventKind
    if typ == "snapshot":
        kind = "snapshot"
    elif typ == "delta":
        kind = "delta"
    else:
        kind = "delta"
    return DepthEvent(kind=kind, bids=bids, asks=asks, u_first=uu, u_final=uu, seq=seq)
