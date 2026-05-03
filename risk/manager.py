"""Stateful soft risk limits: daily PnL, consecutive losses, hourly trade count, drawdown."""

from __future__ import annotations

from collections import deque
from dataclasses import fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from risk.limits import RiskLimits
from risk.safety import safety_check
from strategy.signal import Signal


class RiskManager:
    """Tracks session PnL, enforces soft limits before each trade."""

    def __init__(
        self,
        limits: RiskLimits,
        initial_capital: Decimal,
        *,
        time_fn: Callable[[], float] | None = None,
        utc_now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.limits = limits
        self.initial_capital = initial_capital
        self._time_fn = time_fn
        self._utc_now_fn = utc_now_fn
        self._trade_mono: deque[float] = deque()
        self._utc_day: str = self._current_utc_day()
        self.daily_realized_pnl: Decimal = Decimal("0")
        self.cumulative_realized_pnl: Decimal = Decimal("0")
        self._peak_equity: Decimal = initial_capital
        self.consecutive_losses = int(0)
        self.open_positions: int = 0

    def patch_limits(self, **kwargs: Any) -> None:
        """Replace soft limits at runtime (e.g. Telegram ``/set``). Unknown keys raise."""
        allowed = {f.name for f in fields(RiskLimits)}
        bad = set(kwargs) - allowed
        if bad:
            raise ValueError(f"unknown RiskLimits field(s): {sorted(bad)}")
        self.limits = replace(self.limits, **kwargs)

    def _time(self) -> float:
        import time as _time

        return self._time_fn() if self._time_fn is not None else _time.time()

    def _utc_now(self) -> datetime:
        return self._utc_now_fn() if self._utc_now_fn is not None else datetime.now(timezone.utc)

    def _current_utc_day(self) -> str:
        return self._utc_now().strftime("%Y-%m-%d")

    def _maybe_roll_daily(self) -> None:
        d = self._current_utc_day()
        if d != self._utc_day:
            self._utc_day = d
            self.daily_realized_pnl = Decimal("0")
            self.consecutive_losses = 0

    def _prune_trades_older_than_one_hour(self) -> None:
        now = self._time()
        while self._trade_mono and (now - self._trade_mono[0]) > 3600.0:
            self._trade_mono.popleft()

    def trades_this_hour(self) -> int:
        self._prune_trades_older_than_one_hour()
        return len(self._trade_mono)

    def current_equity(self) -> Decimal:
        return self.initial_capital + self.cumulative_realized_pnl

    def current_drawdown_pct(self) -> Decimal:
        if self._peak_equity <= 0:
            return Decimal("0")
        return (self._peak_equity - self.current_equity()) / self._peak_equity

    def check_pre_trade(
        self,
        signal: Signal,
        *,
        total_capital: Decimal,
    ) -> tuple[bool, str]:
        self._maybe_roll_daily()
        self._prune_trades_older_than_one_hour()

        trade_usd = signal.size * signal.cex_price

        if self.trades_this_hour() >= self.limits.max_trades_per_hour:
            return False, "max_trades_per_hour"
        if trade_usd > self.limits.max_trade_usd:
            return False, "max_trade_usd"
        if total_capital > 0 and trade_usd > total_capital * self.limits.max_trade_pct:
            return False, "max_trade_pct"
        if signal.expected_net_pnl < -self.limits.max_loss_per_trade_usd:
            return False, "expected_loss_exceeds_per_trade_cap"
        if self.daily_realized_pnl <= -self.limits.max_daily_loss_usd:
            return False, "max_daily_loss"
        if self.consecutive_losses >= self.limits.consecutive_loss_limit:
            return False, "consecutive_loss_limit"
        if self.current_drawdown_pct() > self.limits.max_drawdown_pct:
            return False, "max_drawdown_pct"
        if self.open_positions >= self.limits.max_open_positions:
            return False, "max_open_positions"
        base_exposure_usd = trade_usd
        if base_exposure_usd > self.limits.max_position_per_token_usd:
            return False, "max_position_per_token"

        ok, msg = safety_check(
            trade_usd,
            self.daily_realized_pnl,
            total_capital,
            self.trades_this_hour(),
        )
        if not ok:
            return False, f"safety:{msg}"
        return True, "OK"

    def record_trade(self, net_pnl: Decimal | None) -> None:
        """Record a completed trade (``None`` = skip, e.g. not filled)."""
        if net_pnl is None:
            return
        self._maybe_roll_daily()
        self._trade_mono.append(self._time())
        self.daily_realized_pnl += net_pnl
        self.cumulative_realized_pnl += net_pnl
        eq = self.current_equity()
        if eq > self._peak_equity:
            self._peak_equity = eq
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
