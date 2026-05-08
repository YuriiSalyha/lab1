"""Pre-scoring signal checks (excludes score — scoring runs later).

Negative-PnL trades are rejected by default. Setting
**``ARB_ALLOW_NEGATIVE_PNL_USD``** to a non-positive Decimal (e.g. ``-0.10``)
lets a small expected loss pass — used for live demo runs where rebalancing
gas is more expensive than the loss itself. Positive values are rejected so
the env cannot accidentally **tighten** beyond the legacy ``> 0`` rule.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation

from strategy.signal import Signal

_ENV_ALLOW_NEGATIVE_PNL_USD = "ARB_ALLOW_NEGATIVE_PNL_USD"


def _negative_pnl_floor() -> Decimal:
    raw = os.getenv(_ENV_ALLOW_NEGATIVE_PNL_USD, "").strip()
    if not raw:
        return Decimal("0")
    try:
        v = Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return v if v < 0 else Decimal("0")


class PreTradeValidator:
    def validate_signal(self, signal: Signal) -> tuple[bool, str]:
        if time.time() >= signal.expiry:
            return False, "expired"
        if not signal.inventory_ok:
            return False, "inventory"
        if not signal.within_limits:
            return False, "notional_limit"
        floor = _negative_pnl_floor()
        if floor < 0:
            if signal.expected_net_pnl < floor:
                return False, "below_negative_pnl_floor"
        else:
            if signal.expected_net_pnl <= Decimal("0"):
                return False, "non_positive_expected_net_pnl"
        return True, "OK"
