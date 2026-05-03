"""Balance verification helpers and post-trade check."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from inventory.tracker import InventoryTracker, Venue
from scripts.arb_bot import (
    ArbBot,
    ArbBotConfig,
    _balance_tolerance,
    _cex_free_decimal,
)


def test_cex_free_decimal_parses_nested_free() -> None:
    bal = {"ETH": {"free": "1.25"}, "USDT": {"free": 100}}
    assert _cex_free_decimal(bal, "ETH") == Decimal("1.25")
    assert _cex_free_decimal(bal, "usdt") == Decimal("100")
    assert _cex_free_decimal(bal, "MISSING") == Decimal("0")


def test_balance_tolerance_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_BALANCE_VERIFY_TOLERANCE", raising=False)
    assert _balance_tolerance() == Decimal("0.001")


def test_verify_balances_detects_cex_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.delenv("ARB_BALANCE_VERIFY_DISABLED", raising=False)
    monkeypatch.delenv("ARB_BALANCE_VERIFY_TOLERANCE", raising=False)
    cfg = ArbBotConfig(demo=True, dry_run=False, simulation=True)
    bot = ArbBot(cfg)
    bot.config.demo = False
    bot.inventory = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    bot.inventory.update_from_cex(
        Venue.BINANCE,
        {"ETH": {"free": Decimal("10"), "used": Decimal("0"), "total": Decimal("10")}},
    )
    bot.exchange = MagicMock()
    bot.exchange.fetch_balance.return_value = {
        "ETH": {"free": Decimal("99"), "used": Decimal("0"), "total": Decimal("99")},
    }
    bot._fetch_wallet_balances = MagicMock(return_value={})  # noqa: SLF001
    bot._telegram = MagicMock()
    bot._telegram.enabled = False
    bot.running = True
    result = asyncio.run(bot.verify_balances_post_trade("ETH/USDT"))
    assert result == "mismatch_cex"
    assert bot.running is False
