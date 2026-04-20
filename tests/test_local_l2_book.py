"""Adversarial tests for :mod:`exchange.local_l2_book` and :mod:`exchange.ws_depth_adapters`."""

from __future__ import annotations

import json
from decimal import Decimal

from exchange.local_l2_book import SIDE_BID, LocalL2Book
from exchange.orderbook_ws_runner import BinanceDepthSync, BybitSeqSync
from exchange.ws_depth_adapters import (
    DepthEvent,
    parse_binance_depth_json,
    parse_bybit_orderbook_json,
)


def test_delta_removes_level_with_zero_qty() -> None:
    b = LocalL2Book()
    b.apply_snapshot([("100", "1.0")], [("101", "2.0")], sequence_id=1)
    b.apply_delta([("100", "0")], SIDE_BID)
    assert b.best_bid() is None
    assert b.best_ask() is not None


def test_delta_duplicate_price_replaces() -> None:
    b = LocalL2Book()
    b.apply_snapshot([("100", "1")], [], sequence_id=1)
    b.apply_delta([("100", "5")], SIDE_BID)
    assert b.best_bid() == (Decimal("100"), Decimal("5"))


def test_empty_snapshot_no_best() -> None:
    b = LocalL2Book()
    b.apply_snapshot([], [], sequence_id=0)
    d = b.to_normalized_dict("ETH/USDT", timestamp_ms=1)
    assert d["best_bid"] is None
    assert d["best_ask"] is None
    assert d["mid_price"] is None


def test_single_side_no_mid() -> None:
    b = LocalL2Book()
    b.apply_snapshot([("50", "1")], [], sequence_id=0)
    d = b.to_normalized_dict("X/Y", None)
    assert d["mid_price"] is None


def test_large_decimal_prices() -> None:
    b = LocalL2Book()
    p = Decimal("123456789.123456789")
    b.apply_snapshot([(p, "0.0001")], [(p + 1, "0.0001")], sequence_id=None)
    d = b.to_normalized_dict("ETH/USDT", 0)
    assert d["spread_bps"] is not None


def test_parse_binance_wrapped_depth() -> None:
    raw = json.dumps(
        {
            "stream": "ethusdt@depth",
            "data": {
                "e": "depthUpdate",
                "E": 1_700_000_000_000,
                "s": "ETHUSDT",
                "U": 100,
                "u": 105,
                "b": [["2000.0", "1.0"]],
                "a": [["2001.0", "2.0"]],
            },
        }
    )
    ev = parse_binance_depth_json(raw)
    assert ev is not None
    assert ev.kind == "delta"
    assert ev.u_first == 100
    assert ev.u_final == 105
    assert len(ev.bids) == 1


def test_parse_binance_invalid_json_returns_none() -> None:
    assert parse_binance_depth_json("not-json") is None


def test_parse_bybit_delta() -> None:
    raw = json.dumps(
        {
            "topic": "orderbook.50.ETHUSDT",
            "type": "delta",
            "ts": 123,
            "data": {"s": "ETHUSDT", "b": [["2000", "1"]], "a": [], "seq": 10, "u": 1},
        }
    )
    ev = parse_bybit_orderbook_json(raw)
    assert ev is not None
    assert ev.seq == 10


def test_binance_sync_skips_stale() -> None:
    sync = BinanceDepthSync(1000)
    ev = DepthEvent(kind="delta", bids=[], asks=[], u_first=500, u_final=900, seq=900)
    assert sync.evaluate(ev) == "skip"


def test_binance_sync_first_containing_l() -> None:
    sync = BinanceDepthSync(1000)
    ev = DepthEvent(kind="delta", bids=[("1", "1")], asks=[], u_first=990, u_final=1010, seq=1010)
    assert sync.evaluate(ev) == "apply"
    assert sync.synced


def test_bybit_seq_gap_resync() -> None:
    s = BybitSeqSync()
    ev1 = DepthEvent(kind="delta", bids=[], asks=[], seq=1)
    assert s.evaluate_delta(ev1) == "apply"
    ev2 = DepthEvent(kind="delta", bids=[], asks=[], seq=5)
    assert s.evaluate_delta(ev2) == "resync"


def test_parse_non_orderbook_returns_none() -> None:
    assert parse_bybit_orderbook_json(json.dumps({"topic": "tickers.BTCUSDT"})) is None
