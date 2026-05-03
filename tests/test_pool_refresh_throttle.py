"""Cadence + safety tests for ArbBot._maybe_refresh_pool_reserves.

The math-only DEX quote in :class:`SignalGenerator` reads pool reserves out
of in-memory :class:`UniswapV2Pair` objects. ``ArbBot`` keeps those reserves
fresh by calling :meth:`PricingEngine.refresh_pool` on a fixed cadence at the
top of every tick. These tests pin the contract of that helper so the DEX
price actually moves between ticks.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from core.types import Address
from scripts.arb_bot import ArbBot, ArbBotConfig


def _bot() -> ArbBot:
    cfg = ArbBotConfig(demo=True, dry_run=True, min_score=Decimal("0"))
    return ArbBot(cfg)


_MOCK_ADDRS = [
    Address("0x1111111111111111111111111111111111111111"),
    Address("0x2222222222222222222222222222222222222222"),
    Address("0x3333333333333333333333333333333333333333"),
    Address("0x4444444444444444444444444444444444444444"),
]


def _attach_mock_engine(bot: ArbBot, n_pools: int = 1) -> MagicMock:
    if n_pools > len(_MOCK_ADDRS):
        raise ValueError(f"add more mock addresses for n_pools={n_pools}")
    addrs = _MOCK_ADDRS[:n_pools]
    engine = MagicMock()
    engine.pools = {a: MagicMock() for a in addrs}
    bot.pricing_engine = engine
    return engine


def test_no_engine_is_a_noop() -> None:
    bot = _bot()
    bot.pricing_engine = None
    bot._maybe_refresh_pool_reserves()


def test_no_pools_is_a_noop() -> None:
    bot = _bot()
    engine = MagicMock()
    engine.pools = {}
    bot.pricing_engine = engine
    bot._maybe_refresh_pool_reserves()
    engine.refresh_pool.assert_not_called()


def test_zero_interval_disables_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_POOL_REFRESH_SECONDS", "0")
    bot = _bot()
    engine = _attach_mock_engine(bot)
    bot._maybe_refresh_pool_reserves()
    engine.refresh_pool.assert_not_called()


def test_refresh_runs_for_every_loaded_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_POOL_REFRESH_SECONDS", "1")
    bot = _bot()
    engine = _attach_mock_engine(bot, n_pools=3)
    bot._last_pool_refresh_mono = 0.0  # force "due"

    bot._maybe_refresh_pool_reserves()

    assert engine.refresh_pool.call_count == 3
    called_with = {c.args[0] for c in engine.refresh_pool.call_args_list}
    assert called_with == set(engine.pools.keys())


def test_refresh_throttled_within_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rapid-fire calls within the window must only refresh once."""
    monkeypatch.setenv("ARB_POOL_REFRESH_SECONDS", "60")
    bot = _bot()
    engine = _attach_mock_engine(bot, n_pools=2)

    bot._maybe_refresh_pool_reserves()
    bot._maybe_refresh_pool_reserves()

    assert engine.refresh_pool.call_count == 2  # one batch of two pools


def test_refresh_handles_per_pool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single pool failing must not abort refresh of the others."""
    monkeypatch.setenv("ARB_POOL_REFRESH_SECONDS", "1")
    bot = _bot()
    engine = _attach_mock_engine(bot, n_pools=3)
    bot._last_pool_refresh_mono = 0.0

    addrs = list(engine.pools.keys())

    def _fake_refresh(addr: Address) -> None:
        if addr == addrs[1]:
            raise RuntimeError("rpc down")

    engine.refresh_pool.side_effect = _fake_refresh

    bot._maybe_refresh_pool_reserves()

    assert engine.refresh_pool.call_count == 3


def test_invalid_interval_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garbage env value must not crash the tick loop."""
    monkeypatch.setenv("ARB_POOL_REFRESH_SECONDS", "not-a-number")
    bot = _bot()
    engine = _attach_mock_engine(bot)
    bot._last_pool_refresh_mono = 0.0

    bot._maybe_refresh_pool_reserves()

    engine.refresh_pool.assert_called_once()
