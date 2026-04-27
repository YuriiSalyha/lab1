"""Unit tests for :mod:`executor.live_dex_leg` policy and early returns."""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from executor.live_dex_leg import LiveDexLegError, _assert_dex_chain, sync_execute_live_dex_leg
from strategy.signal import Direction, Signal


def _signal() -> Signal:
    now = time.time()
    return Signal(
        signal_id="u1",
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


def test_assert_dex_chain_blocks_mainnet_without_flag():
    with pytest.raises(LiveDexLegError, match="mainnet"):
        _assert_dex_chain(1, expected_chain_id=None, allow_mainnet=False)


def test_assert_dex_chain_allows_mainnet_when_configured():
    _assert_dex_chain(1, expected_chain_id=None, allow_mainnet=True)


def test_assert_dex_chain_mismatch():
    with pytest.raises(LiveDexLegError, match="mismatch"):
        _assert_dex_chain(5, expected_chain_id=11155111, allow_mainnet=False)


def test_sync_execute_pricing_not_ready():
    pe = SimpleNamespace(route_finder=None, pools=[])
    out = sync_execute_live_dex_leg(
        pricing_engine=pe,
        wallet=SimpleNamespace(address="0x" + "1" * 40),
        token_resolver=lambda p: (_ for _ in ()).throw(AssertionError("resolver not used")),
        signal=_signal(),
        size_base_human=Decimal("0.1"),
        direction=Direction.BUY_CEX_SELL_DEX,
        slippage_bps=Decimal("50"),
        deadline_seconds=300,
        run_preflight=False,
        expected_chain_id=None,
        allow_mainnet=False,
    )
    assert out["success"] is False
    assert out.get("error") == "pricing_engine_not_ready"
