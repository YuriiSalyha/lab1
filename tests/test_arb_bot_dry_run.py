"""Dry-run must not invoke executor."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.arb_bot import ArbBot, ArbBotConfig, _resolve_live_fee_structure


def test_dry_run_never_calls_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MAX_TRADE_USD", "100000")
    cfg = ArbBotConfig(demo=True, dry_run=True, min_score=Decimal("0"))
    bot = ArbBot(cfg)
    mock_ex = AsyncMock()

    async def _fail(*_a, **_k):
        raise AssertionError("execute must not be called in dry_run")

    mock_ex.side_effect = _fail
    bot.executor.execute = mock_ex
    asyncio.run(bot._tick())
    mock_ex.assert_not_awaited()


def test_resolve_live_fee_structure_manual_bps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_CEX_TAKER_BPS", "12")
    ex = MagicMock()
    fs = _resolve_live_fee_structure(ex, ["ETH/USDT"])
    assert fs.cex_taker_bps == Decimal("12")
    ex.max_taker_fee_bps_for_symbols.assert_not_called()


def test_resolve_live_fee_structure_from_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_CEX_TAKER_BPS", raising=False)
    ex = MagicMock()
    ex.max_taker_fee_bps_for_symbols.return_value = Decimal("9")
    fs = _resolve_live_fee_structure(ex, ["ETH/USDT", "BTC/USDT"])
    assert fs.cex_taker_bps == Decimal("9")
    ex.max_taker_fee_bps_for_symbols.assert_called_once_with(["ETH/USDT", "BTC/USDT"])


def test_resolve_live_fee_structure_invalid_manual_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARB_CEX_TAKER_BPS", "not_a_number")
    ex = MagicMock()
    ex.max_taker_fee_bps_for_symbols.return_value = Decimal("8")
    fs = _resolve_live_fee_structure(ex, ["ETH/USDT"])
    assert fs.cex_taker_bps == Decimal("8")


def test_resolve_live_fee_structure_default_when_fetch_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARB_CEX_TAKER_BPS", raising=False)
    ex = MagicMock()
    ex.max_taker_fee_bps_for_symbols.return_value = None
    fs = _resolve_live_fee_structure(ex, ["ETH/USDT"])
    assert fs.cex_taker_bps == Decimal("10")
