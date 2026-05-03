"""Pre-scoring signal checks (excludes score — scoring runs later)."""

from __future__ import annotations

import time
from decimal import Decimal

from strategy.signal import Signal


class PreTradeValidator:
    def validate_signal(self, signal: Signal) -> tuple[bool, str]:
        if time.time() >= signal.expiry:
            return False, "expired"
        if not signal.inventory_ok:
            return False, "inventory"
        if not signal.within_limits:
            return False, "notional_limit"
        if signal.expected_net_pnl <= Decimal("0"):
            return False, "non_positive_expected_net_pnl"
        return True, "OK"
