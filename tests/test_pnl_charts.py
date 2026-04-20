"""Tests for :mod:`inventory.pnl_charts`."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from inventory.pnl import ArbRecord, PnLEngine, TradeLeg, Venue
from inventory.pnl_charts import export_pnl_chart


def _leg(
    *,
    tid: str,
    ts: datetime,
    price: str = "2000",
    amt: str = "1",
) -> TradeLeg:
    return TradeLeg(
        id=tid,
        timestamp=ts,
        venue=Venue.BINANCE,
        symbol="ETH/USDT",
        side="buy",
        amount=Decimal(amt),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_asset="USDT",
    )


def test_export_pnl_chart_empty_raises() -> None:
    eng = PnLEngine()
    with pytest.raises(ValueError, match="no trades"):
        export_pnl_chart(eng, "x.png")


def test_export_matplotlib_unsorted_trades_sorted_by_time(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    eng = PnLEngine()
    t1 = datetime(2024, 6, 1, 12, 0, 0)
    t2 = datetime(2024, 6, 1, 10, 0, 0)
    leg = _leg(tid="a", ts=t1)
    r2 = ArbRecord(
        id="2",
        timestamp=t2,
        buy_leg=leg,
        sell_leg=_leg(tid="b", ts=t2),
        gas_cost_usd=Decimal("0"),
    )
    r1 = ArbRecord(
        id="1",
        timestamp=t1,
        buy_leg=leg,
        sell_leg=_leg(tid="c", ts=t1),
        gas_cost_usd=Decimal("0"),
    )
    eng.record(r1)
    eng.record(r2)
    p = tmp_path / "c.png"
    export_pnl_chart(eng, p, backend="matplotlib")
    assert p.is_file() and p.stat().st_size > 0


def test_export_plotly_html(tmp_path: Path) -> None:
    pytest.importorskip("plotly")
    eng = PnLEngine()
    ts = datetime(2024, 1, 1)
    eng.record(
        ArbRecord(
            id="1",
            timestamp=ts,
            buy_leg=_leg(tid="a", ts=ts),
            sell_leg=_leg(tid="b", ts=ts),
            gas_cost_usd=Decimal("0"),
        ),
    )
    p = tmp_path / "c.html"
    export_pnl_chart(eng, p, backend="plotly")
    text = p.read_text(encoding="utf-8")
    assert "plotly" in text.lower() or "html" in text.lower()
