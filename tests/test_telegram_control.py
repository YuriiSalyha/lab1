"""Unit tests for :mod:`monitoring.telegram_control`."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from monitoring.telegram_control import (
    _normalize_command,
    apply_slash_command,
    format_trade_failed_telegram,
    poll_telegram_commands,
)
from risk.limits import RiskLimits
from risk.manager import RiskManager


def test_normalize_command_strips_bot_suffix() -> None:
    assert _normalize_command("/pause@SomeBot") == ("pause", [])
    assert _normalize_command("/set min_score 40") == ("set", ["min_score", "40"])


def test_poll_telegram_commands_filters_chat() -> None:
    notifier = MagicMock()
    notifier.bot_token = "t"
    notifier.target_chat_id = "99"
    fake = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {"chat": {"id": 1}, "text": "/help"},
            },
            {
                "update_id": 2,
                "message": {"chat": {"id": 99}, "text": "/status"},
            },
        ],
    }
    with patch("monitoring.telegram_control._get_updates", return_value=fake):
        off, msgs = poll_telegram_commands(notifier, 0, timeout=0)
    assert off == 3
    assert len(msgs) == 1
    assert "/status" in msgs[0][0]


def test_apply_pause_resume() -> None:
    limits = RiskLimits(
        max_trade_usd=Decimal("100000"),
        max_trade_pct=Decimal("1"),
        max_position_per_token_usd=Decimal("100000"),
        max_open_positions=10,
        max_loss_per_trade_usd=Decimal("100000"),
        max_daily_loss_usd=Decimal("100000"),
        max_drawdown_pct=Decimal("1"),
        max_trades_per_hour=100,
        consecutive_loss_limit=3,
    )
    rm = RiskManager(limits, Decimal("100"))
    g = SimpleNamespace(
        min_spread_bps=Decimal("50"),
        min_profit_usd=Decimal("5"),
        max_position_usd=Decimal("10000"),
    )

    class _Bot:
        def __init__(self) -> None:
            self._trading_paused = False
            self.running = True
            self.config = SimpleNamespace(
                min_score=Decimal("60"),
                tick_seconds=1.0,
                max_signals_per_tick=1,
                dry_run=False,
            )
            self.generator = g
            self.risk_manager = rm

        def stop(self) -> None:
            self.running = False

    bot = _Bot()
    assert "paused" in (apply_slash_command(bot, "/pause") or "").lower()
    assert bot._trading_paused is True
    assert "resumed" in (apply_slash_command(bot, "/resume") or "").lower()
    assert bot._trading_paused is False


def test_format_trade_failed_telegram_escapes() -> None:
    s = format_trade_failed_telegram(
        pair="ETH/USDT",
        error="bad <value>",
        direction="buy_cex",
        size="1",
        spread_bps="50",
    )
    assert "&lt;" in s
    assert "<b>Trade FAILED</b>" in s
