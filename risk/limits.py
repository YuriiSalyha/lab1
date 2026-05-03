"""Soft risk limits (configurable via environment)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from strategy.signal import to_decimal

# Default soft limits (lecture / $100 capital profile)
DEFAULT_MAX_TRADE_USD = Decimal("10")
DEFAULT_MAX_TRADE_PCT = Decimal("0.20")
DEFAULT_MAX_POSITION_PER_TOKEN_USD = Decimal("30")
DEFAULT_MAX_OPEN_POSITIONS = 1
DEFAULT_MAX_LOSS_PER_TRADE_USD = Decimal("5")
DEFAULT_MAX_DAILY_LOSS_USD = Decimal("10")
DEFAULT_MAX_DRAWDOWN_PCT = Decimal("0.20")
DEFAULT_MAX_TRADES_PER_HOUR = 20
DEFAULT_CONSECUTIVE_LOSS_LIMIT = 3

_ENV_MAX_TRADE_USD = "ARB_MAX_TRADE_USD"
_ENV_MAX_TRADE_PCT = "ARB_MAX_TRADE_PCT"
_ENV_MAX_POSITION_PER_TOKEN_USD = "ARB_MAX_POSITION_PER_TOKEN_USD"
_ENV_MAX_OPEN_POSITIONS = "ARB_MAX_OPEN_POSITIONS"
_ENV_MAX_LOSS_PER_TRADE_USD = "ARB_MAX_LOSS_PER_TRADE_USD"
_ENV_MAX_DAILY_LOSS_USD = "ARB_MAX_DAILY_LOSS_USD"
_ENV_MAX_DRAWDOWN_PCT = "ARB_MAX_DRAWDOWN_PCT"
_ENV_MAX_TRADES_PER_HOUR = "ARB_MAX_TRADES_PER_HOUR"
_ENV_CONSECUTIVE_LOSS_LIMIT = "ARB_CONSECUTIVE_LOSS_LIMIT"


def _dec_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return to_decimal(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True)
class RiskLimits:
    max_trade_usd: Decimal
    max_trade_pct: Decimal
    max_position_per_token_usd: Decimal
    max_open_positions: int
    max_loss_per_trade_usd: Decimal
    max_daily_loss_usd: Decimal
    max_drawdown_pct: Decimal
    max_trades_per_hour: int
    consecutive_loss_limit: int

    @classmethod
    def defaults(cls) -> RiskLimits:
        return cls(
            max_trade_usd=DEFAULT_MAX_TRADE_USD,
            max_trade_pct=DEFAULT_MAX_TRADE_PCT,
            max_position_per_token_usd=DEFAULT_MAX_POSITION_PER_TOKEN_USD,
            max_open_positions=DEFAULT_MAX_OPEN_POSITIONS,
            max_loss_per_trade_usd=DEFAULT_MAX_LOSS_PER_TRADE_USD,
            max_daily_loss_usd=DEFAULT_MAX_DAILY_LOSS_USD,
            max_drawdown_pct=DEFAULT_MAX_DRAWDOWN_PCT,
            max_trades_per_hour=DEFAULT_MAX_TRADES_PER_HOUR,
            consecutive_loss_limit=DEFAULT_CONSECUTIVE_LOSS_LIMIT,
        )

    @classmethod
    def from_env(cls) -> RiskLimits:
        return cls(
            max_trade_usd=_dec_env(_ENV_MAX_TRADE_USD, DEFAULT_MAX_TRADE_USD),
            max_trade_pct=_dec_env(_ENV_MAX_TRADE_PCT, DEFAULT_MAX_TRADE_PCT),
            max_position_per_token_usd=_dec_env(
                _ENV_MAX_POSITION_PER_TOKEN_USD,
                DEFAULT_MAX_POSITION_PER_TOKEN_USD,
            ),
            max_open_positions=_int_env(_ENV_MAX_OPEN_POSITIONS, DEFAULT_MAX_OPEN_POSITIONS),
            max_loss_per_trade_usd=_dec_env(
                _ENV_MAX_LOSS_PER_TRADE_USD,
                DEFAULT_MAX_LOSS_PER_TRADE_USD,
            ),
            max_daily_loss_usd=_dec_env(_ENV_MAX_DAILY_LOSS_USD, DEFAULT_MAX_DAILY_LOSS_USD),
            max_drawdown_pct=_dec_env(_ENV_MAX_DRAWDOWN_PCT, DEFAULT_MAX_DRAWDOWN_PCT),
            max_trades_per_hour=_int_env(_ENV_MAX_TRADES_PER_HOUR, DEFAULT_MAX_TRADES_PER_HOUR),
            consecutive_loss_limit=_int_env(
                _ENV_CONSECUTIVE_LOSS_LIMIT,
                DEFAULT_CONSECUTIVE_LOSS_LIMIT,
            ),
        )
