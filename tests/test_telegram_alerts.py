"""Telegram notifier must swallow errors."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from monitoring.telegram_alerts import TelegramNotifier


def test_telegram_disabled_no_network() -> None:
    n = TelegramNotifier(bot_token="", chat_id="")
    n.send("hello")


def test_telegram_send_swallows_url_error() -> None:
    n = TelegramNotifier(bot_token="x", chat_id="1")
    with patch("monitoring.telegram_alerts.urllib.request.urlopen") as u:
        u.side_effect = OSError("boom")
        n.send("hi")


def test_telegram_send_success_path() -> None:
    n = TelegramNotifier(bot_token="t", chat_id="1")

    @contextmanager
    def fake_urlopen(*_a, **_k):
        resp = MagicMock()
        resp.read.return_value = b"{}"
        yield resp

    with patch("monitoring.telegram_alerts.urllib.request.urlopen", fake_urlopen):
        n.send("ok")
