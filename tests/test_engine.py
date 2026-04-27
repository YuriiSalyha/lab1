"""Adversarial tests for the executor state machine.

These drive every state transition: success, CEX timeout, DEX failure with
unwind, partial-fill rejection, circuit breaker gating, and replay protection.
No ``pytest-asyncio`` dependency — each test wraps its coroutine with
``asyncio.run`` so the suite runs on a bare pytest install.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable

import pytest

from executor.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from executor.engine import (
    VENUE_CEX,
    VENUE_DEX,
    Executor,
    ExecutorConfig,
    ExecutorState,
)
from strategy.fees import FeeStructure
from strategy.signal import Direction, Signal


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


# --- Helpers -----------------------------------------------------------------


def _make_signal(
    direction: Direction = Direction.BUY_CEX_SELL_DEX,
    size: Decimal = Decimal("0.1"),
    cex_price: Decimal = Decimal("2000"),
    dex_price: Decimal = Decimal("2020"),
) -> Signal:
    now = time.time()
    return Signal(
        signal_id=f"SIG_{id(object())}",
        pair="ETH/USDT",
        direction=direction,
        cex_price=cex_price,
        dex_price=dex_price,
        spread_bps=Decimal("100"),
        size=size,
        expected_gross_pnl=Decimal("2"),
        expected_fees=Decimal("0.5"),
        expected_net_pnl=Decimal("1.5"),
        score=Decimal("80"),
        timestamp=now,
        expiry=now + 10.0,
        inventory_ok=True,
        within_limits=True,
    )


def _build_executor(**config_kwargs: Any) -> Executor:
    cfg = ExecutorConfig(simulation_mode=True, **config_kwargs)
    # Zero gas keeps fees purely bps so test-sized notionals stay profitable.
    fees = FeeStructure(
        cex_taker_bps=Decimal("10"),
        dex_swap_bps=Decimal("30"),
        gas_cost_usd=Decimal("0"),
    )
    return Executor(
        exchange_client=None,
        pricing_module=None,
        inventory_tracker=None,
        config=cfg,
        fees=fees,
    )


def _patch(ex: Executor, name: str, fn: Callable[..., Any]) -> None:
    setattr(ex, name, fn)


# --- Happy path --------------------------------------------------------------


def test_execute_success():
    ex = _build_executor()
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.DONE
    assert ctx.actual_net_pnl is not None
    assert isinstance(ctx.actual_net_pnl, Decimal)
    assert ctx.leg1_venue == VENUE_CEX
    assert ctx.leg2_venue == VENUE_DEX


def test_pnl_uses_decimal():
    ex = _build_executor()
    ctx = _run(ex.execute(_make_signal()))
    assert isinstance(ctx.leg1_fill_price, Decimal)
    assert isinstance(ctx.leg2_fill_price, Decimal)
    assert isinstance(ctx.actual_net_pnl, Decimal)


def test_dex_first_path():
    """When ``use_flashbots=True``, leg1 is DEX and leg2 is CEX."""
    ex = _build_executor(use_flashbots=True)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.DONE
    assert ctx.leg1_venue == VENUE_DEX
    assert ctx.leg2_venue == VENUE_CEX


# --- Timeouts ----------------------------------------------------------------


def test_execute_cex_timeout():
    ex = _build_executor(leg1_timeout=0.01)

    async def slow_cex(signal, size):
        await asyncio.sleep(1.0)
        return {"success": True, "price": signal.cex_price, "filled": size}

    _patch(ex, "_execute_cex_leg", slow_cex)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.FAILED
    assert "CEX timeout" in (ctx.error or "")


def test_execute_dex_timeout_unwinds():
    ex = _build_executor(leg2_timeout=0.01)

    async def slow_dex(signal, size):
        await asyncio.sleep(1.0)
        return {"success": True, "price": signal.dex_price, "filled": size}

    unwind_calls = {"n": 0}

    async def fake_unwind(ctx):
        unwind_calls["n"] += 1

    _patch(ex, "_execute_dex_leg", slow_dex)
    _patch(ex, "_unwind", fake_unwind)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.FAILED
    assert "unwound" in (ctx.error or "")
    assert ctx.was_unwound is True
    assert unwind_calls["n"] == 1


# --- Failures + unwind -------------------------------------------------------


def test_execute_dex_failure_unwinds():
    ex = _build_executor()

    async def failing_dex(signal, size):
        return {
            "success": False,
            "price": signal.dex_price,
            "filled": Decimal("0"),
            "error": "slippage",
        }

    unwound = {"called": False}

    async def fake_unwind(ctx):
        unwound["called"] = True

    _patch(ex, "_execute_dex_leg", failing_dex)
    _patch(ex, "_unwind", fake_unwind)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.FAILED
    assert ctx.was_unwound is True
    assert unwound["called"]
    assert "unwound" in (ctx.error or "")


def test_partial_fill_rejected():
    ex = _build_executor(min_fill_ratio=Decimal("0.8"))
    sig = _make_signal()

    async def partial_cex(signal, size):
        return {"success": True, "price": signal.cex_price, "filled": size * Decimal("0.5")}

    dex_called = {"n": 0}

    async def dex(signal, size):
        dex_called["n"] += 1
        return {"success": True, "price": signal.dex_price, "filled": size}

    _patch(ex, "_execute_cex_leg", partial_cex)
    _patch(ex, "_execute_dex_leg", dex)
    ctx = _run(ex.execute(sig))
    assert ctx.state == ExecutorState.FAILED
    assert "Partial fill" in (ctx.error or "")
    # Second leg must not run when first leg is below the min-fill threshold.
    assert dex_called["n"] == 0


# --- Circuit breaker + replay ------------------------------------------------


def test_circuit_breaker_blocks():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=1,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    cb.record_failure()
    assert cb.is_open()

    ex = Executor(
        None,
        None,
        None,
        ExecutorConfig(simulation_mode=True),
        fees=FeeStructure(),
        circuit_breaker=cb,
    )

    async def should_not_run(*args, **kwargs):
        raise AssertionError("leg should not run when breaker open")

    _patch(ex, "_execute_cex_leg", should_not_run)
    _patch(ex, "_execute_dex_leg", should_not_run)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.FAILED
    assert "Circuit breaker" in (ctx.error or "")


def test_replay_protection():
    ex = _build_executor()
    sig = _make_signal()
    first = _run(ex.execute(sig))
    second = _run(ex.execute(sig))
    assert first.state == ExecutorState.DONE
    assert second.state == ExecutorState.FAILED
    assert second.error == "Duplicate signal"


def test_invalid_signal_rejected():
    """Expired signals are caught in VALIDATING and never reach leg1."""
    now = time.time()
    sig = Signal(
        signal_id="EXPIRED",
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("2000"),
        dex_price=Decimal("2020"),
        spread_bps=Decimal("100"),
        size=Decimal("0.1"),
        expected_gross_pnl=Decimal("2"),
        expected_fees=Decimal("0.5"),
        expected_net_pnl=Decimal("1.5"),
        score=Decimal("80"),
        timestamp=now - 100,
        expiry=now - 50,
        inventory_ok=True,
        within_limits=True,
    )
    ex = _build_executor()
    ctx = _run(ex.execute(sig))
    assert ctx.state == ExecutorState.FAILED
    assert ctx.error.startswith("Signal invalid")


def test_success_records_breaker_success():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=3,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    cb.record_failure()
    assert cb.current_failures() == 1
    ex = Executor(
        None,
        None,
        None,
        ExecutorConfig(simulation_mode=True),
        fees=FeeStructure(),
        circuit_breaker=cb,
    )
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.DONE
    assert cb.current_failures() == 0


def test_failure_records_breaker_failure():
    cb = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=3,
            window_seconds=60,
            cooldown_seconds=60,
        )
    )
    ex = Executor(
        None,
        None,
        None,
        ExecutorConfig(simulation_mode=True),
        fees=FeeStructure(),
        circuit_breaker=cb,
    )

    async def dex_fail(signal, size):
        return {"success": False, "price": signal.dex_price, "filled": Decimal("0"), "error": "x"}

    async def noop_unwind(ctx):
        return None

    _patch(ex, "_execute_dex_leg", dex_fail)
    _patch(ex, "_unwind", noop_unwind)
    ctx = _run(ex.execute(_make_signal()))
    assert ctx.state == ExecutorState.FAILED
    assert cb.current_failures() == 1


def test_executor_config_rejects_bad_input():
    with pytest.raises(ValueError):
        ExecutorConfig(leg1_timeout=0)
    with pytest.raises(ValueError):
        ExecutorConfig(min_fill_ratio=Decimal("0"))
    with pytest.raises(ValueError):
        ExecutorConfig(min_fill_ratio=Decimal("1.5"))
    with pytest.raises(ValueError):
        ExecutorConfig(dex_deadline_seconds=0)
    with pytest.raises(ValueError):
        ExecutorConfig(dex_slippage_bps=Decimal("10001"))


def test_live_dex_leg_returns_error_when_not_configured():
    """Non-simulation DEX without wallet/resolver must fail closed, not raise."""
    ex = Executor(
        None,
        None,
        None,
        ExecutorConfig(simulation_mode=False),
        fees=FeeStructure(),
    )
    sig = _make_signal()

    async def run():
        return await ex._execute_dex_leg(sig, sig.size)

    out = _run(run())
    assert out["success"] is False
    assert out.get("error") == "dex_wallet_or_resolver_missing"


def test_pnl_sign_follows_direction():
    """For BUY_DEX_SELL_CEX, a higher CEX fill than DEX yields positive PnL."""
    ex = _build_executor()
    sig = _make_signal(
        direction=Direction.BUY_DEX_SELL_CEX, cex_price=Decimal("2020"), dex_price=Decimal("2000")
    )

    async def dex_fill(signal, size):
        return {"success": True, "price": Decimal("2000"), "filled": size}

    async def cex_fill(signal, size):
        return {"success": True, "price": Decimal("2020"), "filled": size}

    _patch(ex, "_execute_dex_leg", dex_fill)
    _patch(ex, "_execute_cex_leg", cex_fill)
    ctx = _run(ex.execute(sig))
    assert ctx.state == ExecutorState.DONE
    assert ctx.actual_net_pnl > Decimal("0")
