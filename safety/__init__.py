"""Course-facing ``safety`` package — re-exports live controls from :mod:`risk`.

Use either ``import safety`` or ``import risk``; implementation lives under ``risk/``.
"""

from __future__ import annotations

from risk.kill_switch import default_kill_switch_path, is_kill_switch_active
from risk.limits import RiskLimits
from risk.manager import RiskManager
from risk.pre_trade import PreTradeValidator
from risk.safety import (
    ABSOLUTE_MAX_DAILY_LOSS,
    ABSOLUTE_MAX_TRADE_USD,
    ABSOLUTE_MAX_TRADES_PER_HOUR,
    ABSOLUTE_MIN_CAPITAL,
    safety_check,
)

__all__ = [
    "ABSOLUTE_MAX_DAILY_LOSS",
    "ABSOLUTE_MAX_TRADE_USD",
    "ABSOLUTE_MAX_TRADES_PER_HOUR",
    "ABSOLUTE_MIN_CAPITAL",
    "RiskLimits",
    "RiskManager",
    "PreTradeValidator",
    "default_kill_switch_path",
    "is_kill_switch_active",
    "safety_check",
]
