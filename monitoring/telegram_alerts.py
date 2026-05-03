"""Telegram notifications via Bot API (stdlib urllib only)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from decimal import Decimal

logger = logging.getLogger(__name__)

TELEGRAM_API_TIMEOUT_S = 10.0
TELEGRAM_SEND_MESSAGE_PATH = "sendMessage"
_ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
_ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
_USER_AGENT = "lab1-telegram-alerts/1.0"


class TelegramNotifier:
    """Best-effort Telegram sender; methods never raise to callers."""

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self._token = (bot_token or os.getenv(_ENV_BOT_TOKEN, "").strip()) or None
        raw_chat = chat_id or os.getenv(_ENV_CHAT_ID, "").strip()
        self._chat_id: str | None = raw_chat if raw_chat else None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def bot_token(self) -> str | None:
        """Read-only token for :mod:`monitoring.telegram_control` polling."""
        return self._token

    @property
    def target_chat_id(self) -> str | None:
        """Configured chat id string (compare to ``message["chat"]["id"]`` as str)."""
        return self._chat_id

    def send(self, message: str, *, urgent: bool = False, parse_mode: str = "HTML") -> None:
        if not self.enabled:
            return
        text = f"URGENT: {message}" if urgent else message
        url = f"https://api.telegram.org/bot{self._token}/{TELEGRAM_SEND_MESSAGE_PATH}"
        body = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
            },
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TELEGRAM_API_TIMEOUT_S) as resp:
                _ = resp.read(512)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                if e.fp is not None:
                    detail = e.fp.read(512).decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning(
                "telegram HTTP %s: %s%s",
                e.code,
                e.reason,
                f" — {detail}" if detail else "",
            )
        except urllib.error.URLError as e:
            logger.warning("telegram URL error: %s", e.reason)
        except Exception as e:
            logger.warning("telegram send failed: %s", e)

    def send_decimal_line(self, prefix: str, value: Decimal) -> None:
        self.send(f"{prefix} <code>{value}</code>")


def html_escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
