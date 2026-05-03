"""Telegram Bot API long-poll + slash commands for :class:`scripts.arb_bot.ArbBot`.

Opt-in via ``TELEGRAM_CONTROLS_ENABLED=1`` (requires ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``).
Only messages whose ``chat.id`` matches the configured chat are honored.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Optional

from monitoring.telegram_alerts import TelegramNotifier, html_escape_text

logger = logging.getLogger(__name__)

_TELEGRAM_GET_UPDATES = "getUpdates"
_USER_AGENT = "lab1-telegram-control/1.0"
_GET_UPDATES_TIMEOUT_S = 5.0

HELP_TEXT = (
    "<b>Commands</b>\n"
    "/help — this list\n"
    "/status — paused, risk, generator, tick\n"
    "/pause — stop taking new trades (bot keeps running)\n"
    "/resume — allow trades again\n"
    "/stop_bot — exit main loop (same as Ctrl+C)\n"
    "/set &lt;key&gt; &lt;value&gt; — runtime config (see docs)\n"
    "Keys: <code>consecutive_loss_limit</code>, <code>min_score</code>, "
    "<code>min_spread_bps</code>, <code>min_profit_usd</code>, "
    "<code>max_position_usd</code>, <code>max_signals_per_tick</code>, "
    "<code>tick_seconds</code>, <code>max_trades_per_hour</code>, "
    "<code>max_daily_loss_usd</code>, <code>max_trade_usd</code>"
)


def _get_updates(token: str, offset: int, timeout: int = 0) -> dict[str, Any]:
    q = urllib.parse.urlencode({"offset": offset, "timeout": max(0, timeout)})
    url = f"https://api.telegram.org/bot{token}/{_TELEGRAM_GET_UPDATES}?{q}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=_GET_UPDATES_TIMEOUT_S) as resp:
        raw = resp.read(65536)
    return json.loads(raw.decode("utf-8"))


def _normalize_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    head = parts[0]
    if head.startswith("/"):
        head = head[1:]
    if "@" in head:
        head, _bot = head.split("@", 1)
    cmd = head.lower()
    return cmd, parts[1:]


def _chat_ok(msg: dict[str, Any], expected_chat: str) -> bool:
    try:
        cid = msg.get("chat", {}).get("id")
    except Exception:
        return False
    return str(cid) == str(expected_chat).strip()


def poll_telegram_commands(
    notifier: TelegramNotifier,
    last_offset: int,
    *,
    timeout: int = 0,
) -> tuple[int, list[tuple[str, str]]]:
    """
    Fetch updates; return ``(next_offset, [(text, update_id_str), ...])`` for allowed chat only.
    """
    if not notifier.bot_token or not notifier.target_chat_id:
        return last_offset, []
    try:
        data = _get_updates(notifier.bot_token, last_offset, timeout=timeout)
    except urllib.error.HTTPError as e:
        logger.warning("telegram getUpdates HTTP %s: %s", e.code, e.reason)
        return last_offset, []
    except urllib.error.URLError as e:
        logger.warning("telegram getUpdates URL error: %s", e.reason)
        return last_offset, []
    except Exception as e:
        logger.warning("telegram getUpdates failed: %s", e)
        return last_offset, []

    if not data.get("ok"):
        return last_offset, []

    out: list[tuple[str, str]] = []
    next_off = last_offset
    for upd in data.get("result", []):
        uid = int(upd.get("update_id", 0))
        next_off = max(next_off, uid + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            continue
        if not _chat_ok(msg, notifier.target_chat_id or ""):
            continue
        text = msg.get("text") or ""
        if not isinstance(text, str) or not text.strip().startswith("/"):
            continue
        out.append((text, str(uid)))

    return next_off, out


def format_trade_success_telegram(
    *,
    pair: str,
    leg_pnl: Decimal,
    cumulative: Decimal,
    metrics_line: str,
) -> str:
    return (
        "<b>Trade OK</b>\n"
        f"pair: <code>{html_escape_text(pair)}</code>\n"
        f"net_pnl: <code>{html_escape_text(str(leg_pnl))}</code>\n"
        f"session_cumulative: <code>{html_escape_text(str(cumulative))}</code>\n"
        f"<code>{html_escape_text(metrics_line)}</code>"
    )


def format_trade_failed_telegram(
    *,
    pair: str,
    error: str,
    direction: str,
    size: str,
    spread_bps: str,
) -> str:
    return (
        "<b>Trade FAILED</b>\n"
        f"pair: <code>{html_escape_text(pair)}</code>\n"
        f"error: <code>{html_escape_text(error)}</code>\n"
        f"dir: <code>{html_escape_text(direction)}</code> "
        f"size: <code>{html_escape_text(size)}</code> "
        f"spread_bps: <code>{html_escape_text(spread_bps)}</code>"
    )


def apply_slash_command(bot: Any, text: str) -> Optional[str]:
    """
    Apply one user message; return HTML reply text or ``None`` if ignored.

    ``bot`` is :class:`scripts.arb_bot.ArbBot` (duck-typed to avoid import cycles).
    """
    cmd, args = _normalize_command(text)
    if not cmd:
        return None

    if cmd == "help":
        return HELP_TEXT

    if cmd == "status":
        g = bot.generator
        lim = bot.risk_manager.limits
        return (
            "<b>Status</b>\n"
            f"trading_paused: <code>{bot._trading_paused}</code>\n"
            f"running: <code>{bot.running}</code>\n"
            f"dry_run: <code>{bot.config.dry_run}</code>\n"
            f"min_score: <code>{html_escape_text(str(bot.config.min_score))}</code>\n"
            f"tick_s: <code>{html_escape_text(str(bot.config.tick_seconds))}</code>\n"
            f"max_signals_per_tick: <code>{bot.config.max_signals_per_tick}</code>\n"
            f"gen min_spread_bps: <code>{g.min_spread_bps}</code>\n"
            f"gen min_profit_usd: <code>{g.min_profit_usd}</code>\n"
            f"gen max_position_usd: <code>{g.max_position_usd}</code>\n"
            f"risk consecutive_losses: <code>{bot.risk_manager.consecutive_losses}</code> / "
            f"<code>{lim.consecutive_loss_limit}</code>\n"
            f"risk max_trades_per_hour: <code>{lim.max_trades_per_hour}</code>\n"
            f"risk max_daily_loss_usd: <code>{lim.max_daily_loss_usd}</code>\n"
            f"risk max_trade_usd: <code>{lim.max_trade_usd}</code>"
        )

    if cmd == "pause":
        bot._trading_paused = True
        return "Trading <b>paused</b> (no new executions). /resume to continue."

    if cmd == "resume":
        bot._trading_paused = False
        return "Trading <b>resumed</b>."

    if cmd in ("stop_bot", "stop"):
        bot.stop()
        return "Stop requested — main loop will exit."

    if cmd == "set":
        if len(args) < 2:
            return "Usage: <code>/set key value</code> (see /help)"
        key = args[0].strip().lower()
        val_s = args[1].strip()
        return _apply_set_key(bot, key, val_s)

    return f"Unknown command <code>{html_escape_text(cmd)}</code>. Try /help"


def _apply_set_key(bot: Any, key: str, val_s: str) -> str:
    from strategy.signal import to_decimal

    g = bot.generator
    rm = bot.risk_manager

    try:
        if key == "min_score":
            bot.config.min_score = to_decimal(val_s)
            return f"min_score set to <code>{bot.config.min_score}</code>"
        if key == "min_spread_bps":
            g.min_spread_bps = to_decimal(val_s)
            return f"min_spread_bps set to <code>{g.min_spread_bps}</code>"
        if key == "min_profit_usd":
            g.min_profit_usd = to_decimal(val_s)
            return f"min_profit_usd set to <code>{g.min_profit_usd}</code>"
        if key == "max_position_usd":
            g.max_position_usd = to_decimal(val_s)
            return f"max_position_usd set to <code>{g.max_position_usd}</code>"
        if key == "tick_seconds":
            bot.config.tick_seconds = float(val_s)
            return f"tick_seconds set to <code>{bot.config.tick_seconds}</code>"
        if key == "max_signals_per_tick":
            n = max(1, int(val_s))
            bot.config.max_signals_per_tick = n
            return f"max_signals_per_tick set to <code>{n}</code>"
        if key in ("consecutive_loss_limit", "max_trades_per_hour", "max_open_positions"):
            rm.patch_limits(**{key: int(val_s)})
            return f"risk <code>{html_escape_text(key)}</code> updated."
        if key in (
            "max_trade_usd",
            "max_trade_pct",
            "max_position_per_token_usd",
            "max_loss_per_trade_usd",
            "max_daily_loss_usd",
            "max_drawdown_pct",
        ):
            rm.patch_limits(**{key: to_decimal(val_s)})
            return f"risk <code>{html_escape_text(key)}</code> updated."
    except Exception as exc:
        return f"Set failed: <code>{html_escape_text(str(exc))}</code>"

    return f"Unknown key <code>{html_escape_text(key)}</code>. See /help"
