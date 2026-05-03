"""Non-configurable hard ceilings for live trading safety.

These literals are intentionally **not** read from the environment.
"""

from __future__ import annotations

from decimal import Decimal

ABSOLUTE_MAX_TRADE_USD = Decimal("25")
ABSOLUTE_MAX_DAILY_LOSS = Decimal("20")
ABSOLUTE_MIN_CAPITAL = Decimal("65")
ABSOLUTE_MAX_TRADES_PER_HOUR = 30


def safety_check(
    trade_usd: Decimal,
    daily_pnl: Decimal,
    total_capital: Decimal,
    trades_this_hour: int,
) -> tuple[bool, str]:
    """Final gate after soft risk checks. 'daily_pnl' is a sum of today's net PnL"""
    if trade_usd > ABSOLUTE_MAX_TRADE_USD:
        return False, f"trade_usd {trade_usd} exceeds absolute max {ABSOLUTE_MAX_TRADE_USD}"
    if daily_pnl <= -ABSOLUTE_MAX_DAILY_LOSS:
        return False, "absolute daily loss limit reached"
    if total_capital < ABSOLUTE_MIN_CAPITAL:
        return False, f"capital {total_capital} below absolute minimum {ABSOLUTE_MIN_CAPITAL}"
    if trades_this_hour >= ABSOLUTE_MAX_TRADES_PER_HOUR:
        return False, "absolute hourly trade limit reached"
    return True, "OK"
