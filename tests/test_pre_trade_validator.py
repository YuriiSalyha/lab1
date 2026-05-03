"""Pre-trade validator."""

from __future__ import annotations

import time
from decimal import Decimal

from risk.pre_trade import PreTradeValidator
from strategy.signal import Direction, Signal


def _mk_signal(**kwargs) -> Signal:
    base = {
        "pair": "ETH/USDT",
        "direction": Direction.BUY_CEX_SELL_DEX,
        "cex_price": Decimal("2000"),
        "dex_price": Decimal("2001"),
        "spread_bps": Decimal("10"),
        "size": Decimal("1"),
        "expected_gross_pnl": Decimal("5"),
        "expected_fees": Decimal("1"),
        "expected_net_pnl": Decimal("4"),
        "score": Decimal("50"),
        "expiry": time.time() + 60.0,
        "inventory_ok": True,
        "within_limits": True,
    }
    base.update(kwargs)
    return Signal.create(**base)


def test_pre_trade_ok() -> None:
    v = PreTradeValidator()
    ok, msg = v.validate_signal(_mk_signal())
    assert ok and msg == "OK"


def test_pre_trade_expired() -> None:
    v = PreTradeValidator()
    past = time.time() - 100.0
    ok, msg = v.validate_signal(
        _mk_signal(timestamp=past, expiry=past + 30.0),
    )
    assert not ok and msg == "expired"


def test_pre_trade_bad_inventory() -> None:
    v = PreTradeValidator()
    ok, msg = v.validate_signal(_mk_signal(inventory_ok=False))
    assert not ok and msg == "inventory"


def test_pre_trade_non_positive_net() -> None:
    v = PreTradeValidator()
    ok, msg = v.validate_signal(_mk_signal(expected_net_pnl=Decimal("0")))
    assert not ok and msg == "non_positive_expected_net_pnl"
