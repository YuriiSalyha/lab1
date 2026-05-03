"""``safety`` package is a stable alias of :mod:`risk` for course rubrics."""

from __future__ import annotations

import risk.safety as rs
import safety as ss


def test_safety_package_reexports_risk_symbols() -> None:
    assert ss.ABSOLUTE_MAX_TRADE_USD == rs.ABSOLUTE_MAX_TRADE_USD
    assert ss.ABSOLUTE_MAX_DAILY_LOSS == rs.ABSOLUTE_MAX_DAILY_LOSS
    assert ss.ABSOLUTE_MIN_CAPITAL == rs.ABSOLUTE_MIN_CAPITAL
    assert ss.RiskLimits is not None
    assert ss.RiskManager is not None
    assert ss.PreTradeValidator is not None
    assert ss.default_kill_switch_path is not None
    assert ss.is_kill_switch_active is not None
    assert ss.safety_check is rs.safety_check
