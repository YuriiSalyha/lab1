"""Tests for :mod:`executor.circuit_breaker` and :mod:`executor.replay_protection`.

Covers rolling-window failure accumulation, cooldown auto-reset, TTL-based
replay expiry, and edge cases around success/failure interleaving.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from executor.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from executor.replay_protection import DEFAULT_REPLAY_TTL_S, ReplayProtection
from strategy.signal import Direction, Signal


def _signal(signal_id: str = "abc") -> Signal:
    now = time.time()
    return Signal(
        signal_id=signal_id,
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("2000"),
        dex_price=Decimal("2010"),
        spread_bps=Decimal("50"),
        size=Decimal("0.1"),
        expected_gross_pnl=Decimal("1"),
        expected_fees=Decimal("0"),
        expected_net_pnl=Decimal("1"),
        score=Decimal("80"),
        timestamp=now,
        expiry=now + 10,
        inventory_ok=True,
        within_limits=True,
    )


# --- Circuit breaker ---------------------------------------------------------


def test_circuit_breaker_trips():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=3,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open()
    cb.record_failure()
    assert cb.is_open()


def test_circuit_breaker_resets(monkeypatch):
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=1,
            window_seconds=60,
            cooldown_seconds=1,
        )
    )
    start = 1_000.0
    monkeypatch.setattr(time, "time", lambda: start)
    cb.record_failure()
    assert cb.is_open()
    monkeypatch.setattr(time, "time", lambda: start + 2.0)
    assert not cb.is_open()
    # Auto-reset clears internal state.
    assert cb.current_failures() == 0
    assert cb.tripped_at is None


def test_circuit_breaker_window_drops_old_failures(monkeypatch):
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=3,
            window_seconds=10,
            cooldown_seconds=60,
        )
    )
    t = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    cb.record_failure()
    cb.record_failure()
    t["now"] += 100  # window has slid past both failures
    cb.record_failure()
    assert not cb.is_open()
    assert cb.current_failures() == 1


def test_current_failures_reports_count():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=5,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    assert cb.current_failures() == 0
    cb.record_failure()
    assert cb.current_failures() == 1
    cb.record_failure()
    assert cb.current_failures() == 2


def test_record_success_is_leaky():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=5,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    cb.record_failure()
    cb.record_failure()
    assert cb.current_failures() == 2
    cb.record_success()
    assert cb.current_failures() == 1
    cb.record_success()
    cb.record_success()  # extra success on empty queue is a no-op
    assert cb.current_failures() == 0


def test_time_until_reset():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=1,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    assert cb.time_until_reset() == 0
    cb.record_failure()
    remaining = cb.time_until_reset()
    assert 0 < remaining <= 60


def test_circuit_breaker_config_rejects_invalid():
    with pytest.raises(ValueError):
        CircuitBreakerConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreakerConfig(window_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreakerConfig(cooldown_seconds=-1)


def test_failure_threshold_property_exposed():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=7))
    assert cb.failure_threshold == 7


# --- Replay protection -------------------------------------------------------


def test_replay_blocks_duplicate():
    rp = ReplayProtection(ttl_seconds=30)
    s = _signal("sig-1")
    assert not rp.is_duplicate(s)
    rp.mark_executed(s)
    assert rp.is_duplicate(s)


def test_replay_allows_new():
    rp = ReplayProtection(ttl_seconds=30)
    rp.mark_executed(_signal("sig-1"))
    assert not rp.is_duplicate(_signal("sig-2"))


def test_replay_ttl_expires(monkeypatch):
    rp = ReplayProtection(ttl_seconds=10)
    t = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    s = _signal("sig-1")
    rp.mark_executed(s)
    assert rp.is_duplicate(s)
    t["now"] += 100
    assert not rp.is_duplicate(s)


def test_replay_clear_wipes_state():
    rp = ReplayProtection(ttl_seconds=30)
    rp.mark_executed(_signal("a"))
    rp.mark_executed(_signal("b"))
    rp.clear()
    assert not rp.is_duplicate(_signal("a"))
    assert not rp.is_duplicate(_signal("b"))


def test_replay_ttl_validation():
    with pytest.raises(ValueError):
        ReplayProtection(ttl_seconds=0)
    with pytest.raises(ValueError):
        ReplayProtection(ttl_seconds=-5)


def test_default_replay_ttl_exists():
    assert DEFAULT_REPLAY_TTL_S > 0
