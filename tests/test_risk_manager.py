"""Tests for :mod:`risk.manager` and :mod:`risk.limits`."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from risk.limits import RiskLimits
from risk.manager import RiskManager
from strategy.signal import Direction, Signal


def _sig(
    *,
    size: Decimal = Decimal("1"),
    cex_price: Decimal = Decimal("20"),
    expected_net: Decimal = Decimal("10"),
) -> Signal:
    return Signal.create(
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=cex_price,
        dex_price=cex_price + Decimal("1"),
        spread_bps=Decimal("50"),
        size=size,
        expected_gross_pnl=Decimal("20"),
        expected_fees=Decimal("10"),
        expected_net_pnl=expected_net,
        score=Decimal("50"),
        expiry=9_999_999_999.0,
        inventory_ok=True,
        within_limits=True,
    )


def test_risk_manager_hourly_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    t = {"now": 0.0}

    def fake_time() -> float:
        return t["now"]

    limits = RiskLimits(
        max_trade_usd=Decimal("100000"),
        max_trade_pct=Decimal("1"),
        max_position_per_token_usd=Decimal("100000"),
        max_open_positions=10,
        max_loss_per_trade_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("100000"),
        max_drawdown_pct=Decimal("1"),
        max_trades_per_hour=2,
        consecutive_loss_limit=100,
    )
    rm = RiskManager(limits, Decimal("100"), time_fn=fake_time)
    cap = Decimal("100000")
    for _ in range(2):
        ok, _ = rm.check_pre_trade(_sig(), total_capital=cap)
        assert ok
        rm.record_trade(Decimal("1"))
        t["now"] += 1.0
    ok, reason = rm.check_pre_trade(_sig(), total_capital=cap)
    assert not ok and reason == "max_trades_per_hour"


def test_risk_manager_daily_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    day = {"d": datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)}

    def fake_utc() -> datetime:
        return day["d"]

    limits = RiskLimits(
        max_trade_usd=Decimal("100000"),
        max_trade_pct=Decimal("1"),
        max_position_per_token_usd=Decimal("100000"),
        max_open_positions=10,
        max_loss_per_trade_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("5"),
        max_drawdown_pct=Decimal("1"),
        max_trades_per_hour=100,
        consecutive_loss_limit=100,
    )
    rm = RiskManager(limits, Decimal("100"), utc_now_fn=fake_utc)
    rm.record_trade(Decimal("-6"))
    assert rm.daily_realized_pnl == Decimal("-6")
    ok, _ = rm.check_pre_trade(_sig(), total_capital=Decimal("100000"))
    assert not ok
    day["d"] = datetime(2020, 1, 2, 0, 0, tzinfo=timezone.utc)
    ok2, _ = rm.check_pre_trade(_sig(), total_capital=Decimal("100000"))
    assert ok2
    assert rm.daily_realized_pnl == Decimal("0")


def test_consecutive_loss_limit() -> None:
    limits = RiskLimits(
        max_trade_usd=Decimal("100000"),
        max_trade_pct=Decimal("1"),
        max_position_per_token_usd=Decimal("100000"),
        max_open_positions=10,
        max_loss_per_trade_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("100000"),
        max_drawdown_pct=Decimal("1"),
        max_trades_per_hour=100,
        consecutive_loss_limit=2,
    )
    rm = RiskManager(limits, Decimal("100"))
    cap = Decimal("100000")
    rm.record_trade(Decimal("-1"))
    rm.record_trade(Decimal("-1"))
    ok, reason = rm.check_pre_trade(_sig(), total_capital=cap)
    assert not ok and reason == "consecutive_loss_limit"


def test_risk_manager_patch_limits() -> None:
    limits = RiskLimits(
        max_trade_usd=Decimal("5"),
        max_trade_pct=Decimal("0.2"),
        max_position_per_token_usd=Decimal("30"),
        max_open_positions=1,
        max_loss_per_trade_usd=Decimal("5"),
        max_daily_loss_usd=Decimal("10"),
        max_drawdown_pct=Decimal("0.2"),
        max_trades_per_hour=20,
        consecutive_loss_limit=3,
    )
    rm = RiskManager(limits, Decimal("100"))
    rm.patch_limits(consecutive_loss_limit=7)
    assert rm.limits.consecutive_loss_limit == 7
    with pytest.raises(ValueError, match="unknown"):
        rm.patch_limits(**{"not_a_field": 1})


def test_max_trade_usd_soft() -> None:
    limits = RiskLimits(
        max_trade_usd=Decimal("100"),
        max_trade_pct=Decimal("1"),
        max_position_per_token_usd=Decimal("100000"),
        max_open_positions=10,
        max_loss_per_trade_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("100000"),
        max_drawdown_pct=Decimal("1"),
        max_trades_per_hour=100,
        consecutive_loss_limit=100,
    )
    rm = RiskManager(limits, Decimal("100"))
    ok, reason = rm.check_pre_trade(
        _sig(size=Decimal("1"), cex_price=Decimal("200")),
        total_capital=Decimal("100000"),
    )
    assert not ok and reason == "max_trade_usd"
