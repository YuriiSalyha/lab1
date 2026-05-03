"""Boundary tests for :mod:`risk.safety`."""

from __future__ import annotations

from decimal import Decimal

from risk.safety import (
    ABSOLUTE_MAX_DAILY_LOSS,
    ABSOLUTE_MAX_TRADE_USD,
    ABSOLUTE_MAX_TRADES_PER_HOUR,
    ABSOLUTE_MIN_CAPITAL,
    safety_check,
)


def test_safety_trade_usd_at_max_ok() -> None:
    ok, msg = safety_check(
        ABSOLUTE_MAX_TRADE_USD,
        Decimal("0"),
        ABSOLUTE_MIN_CAPITAL,
        0,
    )
    assert ok and msg == "OK"


def test_safety_trade_usd_one_wei_over_rejected() -> None:
    ok, msg = safety_check(
        ABSOLUTE_MAX_TRADE_USD + Decimal("0.01"),
        Decimal("0"),
        ABSOLUTE_MIN_CAPITAL,
        0,
    )
    assert not ok


def test_safety_daily_loss_at_limit_rejected() -> None:
    ok, msg = safety_check(
        Decimal("1"),
        -ABSOLUTE_MAX_DAILY_LOSS,
        ABSOLUTE_MIN_CAPITAL,
        0,
    )
    assert not ok


def test_safety_daily_loss_just_above_limit_ok() -> None:
    ok, msg = safety_check(
        Decimal("1"),
        -ABSOLUTE_MAX_DAILY_LOSS + Decimal("0.01"),
        ABSOLUTE_MIN_CAPITAL,
        0,
    )
    assert ok


def test_safety_capital_below_min_rejected() -> None:
    ok, msg = safety_check(Decimal("1"), Decimal("0"), ABSOLUTE_MIN_CAPITAL - Decimal("0.01"), 0)
    assert not ok


def test_safety_hourly_trade_count_at_limit_rejected() -> None:
    ok, msg = safety_check(
        Decimal("1"),
        Decimal("0"),
        ABSOLUTE_MIN_CAPITAL,
        ABSOLUTE_MAX_TRADES_PER_HOUR,
    )
    assert not ok


def test_safety_hourly_trade_count_one_below_limit_ok() -> None:
    ok, msg = safety_check(
        Decimal("1"),
        Decimal("0"),
        ABSOLUTE_MIN_CAPITAL,
        ABSOLUTE_MAX_TRADES_PER_HOUR - 1,
    )
    assert ok


def test_absolute_constants_match_course_hard_caps() -> None:
    assert ABSOLUTE_MAX_TRADE_USD == Decimal("25")
    assert ABSOLUTE_MAX_DAILY_LOSS == Decimal("20")
    assert ABSOLUTE_MIN_CAPITAL == Decimal("50")


def test_safety_capital_exactly_at_minimum_ok() -> None:
    ok, msg = safety_check(Decimal("1"), Decimal("0"), ABSOLUTE_MIN_CAPITAL, 0)
    assert ok and msg == "OK"


def test_safety_trade_usd_zero_ok() -> None:
    ok, msg = safety_check(Decimal("0"), Decimal("0"), ABSOLUTE_MIN_CAPITAL, 0)
    assert ok and msg == "OK"
