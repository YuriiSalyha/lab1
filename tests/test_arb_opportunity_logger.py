"""Tests for :mod:`inventory.arb_opportunity_logger`."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from inventory.arb_opportunity_logger import (
    ArbOpportunityLogger,
    ArbOpportunityRecord,
    records_to_table_rows,
)


def test_append_from_arb_check_roundtrip(tmp_path: Path) -> None:
    log = ArbOpportunityLogger()
    ts = datetime(2024, 1, 15, 12, 0, 0)
    result = {
        "pair": "ETH/USDT",
        "timestamp": ts,
        "dex_price": Decimal("2000"),
        "cex_bid": Decimal("2010"),
        "cex_ask": Decimal("2005"),
        "gap_bps": Decimal("12.5"),
        "direction": "buy_dex_sell_cex",
        "estimated_costs_bps": Decimal("10"),
        "estimated_net_pnl_bps": Decimal("2.5"),
        "inventory_ok": True,
        "executable": True,
        "details": {},
    }
    log.append_from_arb_check(result, cex_venue="bybit")
    p = tmp_path / "ärbitrage.csv"
    log.export_csv(p)
    text = p.read_text(encoding="utf-8")
    assert "dex_cex" in text
    assert "bybit" in text
    assert "ETH/USDT" in text


def test_duplicate_timestamps_preserved() -> None:
    log = ArbOpportunityLogger()
    ts = datetime.utcnow()
    for i in range(3):
        log.append(
            ArbOpportunityRecord(
                timestamp=ts,
                kind="dex_cex",
                pair="ETH/USDT",
                direction="x",
                gap_bps=Decimal(i),
                estimated_net_pnl_bps=Decimal("0"),
                executable=False,
            ),
        )
    assert len(log.records) == 3


def test_empty_logger_csv_header_only(tmp_path: Path) -> None:
    log = ArbOpportunityLogger()
    p = tmp_path / "empty.csv"
    log.export_csv(p)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "timestamp" in lines[0]


def test_cex_cex_below_threshold_returns_none() -> None:
    log = ArbOpportunityLogger()
    r = log.append_cex_cex_spread(
        symbol_a="ETH/USDT",
        symbol_b="ETH/USDT",
        mid_a=Decimal("3000"),
        mid_b=Decimal("3000.01"),
        venue_a="binance",
        venue_b="bybit",
        min_spread_bps=Decimal("1000"),
    )
    assert r is None


def test_cex_cex_logs_when_wide() -> None:
    log = ArbOpportunityLogger()
    r = log.append_cex_cex_spread(
        symbol_a="ETH/USDT",
        symbol_b="ETH/USDT",
        mid_a=Decimal("3000"),
        mid_b=Decimal("4000"),
        venue_a="binance",
        venue_b="bybit",
        min_spread_bps=Decimal("1"),
    )
    assert r is not None
    assert r.kind == "cex_cex"


def test_records_to_table_rows() -> None:
    snap = {
        "venues": {
            "binance": {"USDT": {"free": "1", "locked": "0", "total": "1"}},
        },
    }
    rows = records_to_table_rows(snap)
    assert rows == [["binance", "USDT", "1", "0", "1"]]


def test_csv_unicode_path(tmp_path: Path) -> None:
    log = ArbOpportunityLogger()
    log.append(
        ArbOpportunityRecord(
            timestamp=datetime.utcnow(),
            kind="dex_cex",
            pair="ETH/USDT",
            direction="d",
            gap_bps=Decimal("1"),
            estimated_net_pnl_bps=Decimal("1"),
            executable=False,
            extra_json='{"note":"测试"}',
        ),
    )
    sub = tmp_path / "подпапка"
    sub.mkdir()
    p = sub / "лог.csv"
    log.export_csv(p)
    r = list(csv.DictReader(p.open(encoding="utf-8")))
    assert r[0]["pair"] == "ETH/USDT"
