"""Trade CSV journal (append-only analysis file)."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from executor.engine import ExecutionContext, ExecutorState
from monitoring.trade_csv_log import (
    TradeCsvJournal,
    build_trade_csv_row,
    trade_csv_path,
)
from strategy.signal import Direction, Signal


def test_trade_csv_path_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_TRADE_CSV_DISABLED", "1")
    assert trade_csv_path() is None


def test_trade_csv_path_default_when_not_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ARB_TRADE_CSV_DISABLED", raising=False)
    monkeypatch.delenv("ARB_TRADE_CSV", raising=False)
    p = trade_csv_path()
    assert p is not None
    assert p.name == "trades_journal.csv"


def test_trade_csv_journal_writes_header_and_row(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    j = TradeCsvJournal(path)
    sig = Signal.create(
        "ETH/USDT",
        Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("2000"),
        dex_price=Decimal("2010"),
        spread_bps=Decimal("50"),
        size=Decimal("0.1"),
        expected_gross_pnl=Decimal("1"),
        expected_fees=Decimal("0.2"),
        expected_net_pnl=Decimal("0.8"),
        score=Decimal("70"),
        expiry=9_999_999_999.0,
        inventory_ok=True,
        within_limits=True,
    )
    j.append_row(
        build_trade_csv_row(
            outcome="dry_run",
            pair="ETH/USDT",
            signal=sig,
            event_mono=sig.timestamp + 0.5,
            config_demo=False,
            config_dry_run=True,
            config_simulation=True,
            production_binance=False,
            min_score=Decimal("60"),
            tick_seconds=1.0,
        ),
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    r = next(csv.DictReader(lines))
    assert r["outcome"] == "dry_run"
    assert r["pair"] == "ETH/USDT"
    assert r["direction"] == Direction.BUY_CEX_SELL_DEX.value


def test_build_trade_csv_row_with_failed_ctx() -> None:
    sig = Signal.create(
        "BTC/USDT",
        Direction.BUY_DEX_SELL_CEX,
        cex_price=Decimal("40000"),
        dex_price=Decimal("39900"),
        spread_bps=Decimal("25"),
        size=Decimal("0.01"),
        expected_gross_pnl=Decimal("1"),
        expected_fees=Decimal("0.5"),
        expected_net_pnl=Decimal("0.5"),
        score=Decimal("80"),
        expiry=9_999_999_999.0,
        inventory_ok=True,
        within_limits=True,
    )
    ctx = ExecutionContext(signal=sig, state=ExecutorState.FAILED, error="DEX timeout")
    row = build_trade_csv_row(
        outcome="executed_failed",
        pair="BTC/USDT",
        signal=sig,
        event_mono=sig.timestamp,
        config_demo=False,
        config_dry_run=False,
        config_simulation=True,
        production_binance=False,
        min_score=Decimal("60"),
        tick_seconds=2.0,
        ctx=ctx,
        error_message="DEX timeout",
    )
    assert row["executor_state"] == "FAILED"
    assert "timeout" in row["error_message"]
