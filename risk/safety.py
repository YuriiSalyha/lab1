"""Non-configurable hard ceilings for live trading safety.

These literals are intentionally **not** read from the environment. The
``total_capital`` argument to :func:`safety_check` is the **combined**
DEX (wallet) + CEX USD value from :func:`inventory.usd_mark.estimate_inventory_usd_live`
in live mode (or :func:`inventory.usd_mark.estimate_inventory_usd` in demo).
Live pricing may raise :exc:`inventory.usd_mark.LiveUsdMarkError` before this
function runs.
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
    """See module docstring for ``total_capital`` semantics."""
    if trade_usd > ABSOLUTE_MAX_TRADE_USD:
        return False, f"trade_usd {trade_usd} exceeds absolute max {ABSOLUTE_MAX_TRADE_USD}"
    if daily_pnl <= -ABSOLUTE_MAX_DAILY_LOSS:
        return False, "absolute daily loss limit reached"
    if total_capital < ABSOLUTE_MIN_CAPITAL:
        return False, f"capital {total_capital} below absolute minimum {ABSOLUTE_MIN_CAPITAL}"
    if trades_this_hour >= ABSOLUTE_MAX_TRADES_PER_HOUR:
        return False, "absolute hourly trade limit reached"
    return True, "OK"
