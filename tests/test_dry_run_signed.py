"""Tests for the signed dry-run pipeline (`ARB_DRY_RUN_MODE=signed`)."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from executor.engine import ExecutionContext, Executor, ExecutorConfig
from executor.live_dex_leg import DRY_RUN_TX_HASH_PREFIX
from scripts.arb_bot import (
    _DRY_RUN_MODE_LOG,
    _DRY_RUN_MODE_SIGNED,
    ArbBot,
    ArbBotConfig,
    _apply_cex_virtual_overrides,
    _parse_virtual_balances,
    format_dryrun_console_line,
    format_dryrun_signed_telegram,
)
from strategy.signal import Direction, Signal


def _signal(spread_bps: Decimal = Decimal("30")) -> Signal:
    now = time.time()
    return Signal(
        signal_id="dryrun-1",
        pair="ETH/USDC",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("3450.85"),
        dex_price=Decimal("3460.40"),
        spread_bps=spread_bps,
        size=Decimal("0.05"),
        expected_gross_pnl=Decimal("3.0"),
        expected_fees=Decimal("1.16"),
        expected_net_pnl=Decimal("1.84"),
        score=Decimal("80"),
        timestamp=now,
        expiry=now + 30,
        inventory_ok=True,
        within_limits=True,
    )


def _snapshot() -> dict[str, Any]:
    return {
        "cex_bid": Decimal("3450.20"),
        "cex_ask": Decimal("3450.85"),
        "cex_bid_size": Decimal("1.42"),
        "cex_ask_size": Decimal("0.91"),
        "cex_mid": Decimal("3450.525"),
        "cex_spread_bps": Decimal("1.88"),
        "dex_buy": Decimal("3450.10"),
        "dex_sell": Decimal("3460.40"),
        "dex_source": "engine",
        "size_probe_base": Decimal("0.01"),
        "fetched_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_virtual_balances_normalises_aliases() -> None:
    parsed = _parse_virtual_balances("ETH=2,USDC=5000,WETH=3,WBTC=0.1,bogus,empty=,negative=-1")
    assert parsed == {
        "ETH": Decimal("3"),  # WETH alias overrides ETH because dict insertion order
        "USDC": Decimal("5000"),
        "BTC": Decimal("0.1"),
    }


def test_console_line_no_signal_uses_best_dex_quote() -> None:
    line = format_dryrun_console_line(
        pair="ETH/USDC",
        snapshot=_snapshot(),
        signal=None,
        sent="NO reason=no_opportunity",
    )
    assert "ETH/USDC" in line
    assert "bid 3450.20 x 1.4200 ETH" in line
    assert "ask 3450.85 x 0.9100 ETH" in line
    assert "dex 3460.40" in line  # picks the side further from mid
    assert "sent=NO reason=no_opportunity" in line


def test_console_line_with_signal_uses_directional_dex_price() -> None:
    line = format_dryrun_console_line(
        pair="ETH/USDC",
        snapshot=_snapshot(),
        signal=_signal(),
        sent="NO (DRY-RUN signed_tx=0xDRYRUNabc)",
    )
    # BUY_CEX_SELL_DEX -> uses dex_sell as the relevant DEX price.
    assert "dex 3460.40" in line
    assert "spread 30.00 bps" in line
    assert "est_profit $1.84" in line
    assert "sent=NO (DRY-RUN signed_tx=0xDRYRUNabc)" in line


def test_console_line_handles_missing_snapshot() -> None:
    line = format_dryrun_console_line(
        pair="ETH/USDC", snapshot=None, signal=None, sent="NO reason=skipped"
    )
    assert "bid N/A" in line
    assert "ask N/A" in line
    assert "dex N/A" in line


def test_dryrun_signed_telegram_contains_signed_marker() -> None:
    body = format_dryrun_signed_telegram(
        pair="ETH/USDC",
        leg_pnl=Decimal("1.84"),
        cumulative=Decimal("1.84"),
        metrics_line="exp=30bps slippage=2bps net=$1.84",
        signed_tx_hash="0xabcdef0123456789",
        raw_tx_hex_preview="0x02f8aa0185...",
        preflight_gas_used=152_345,
    )
    assert "[DRY-RUN] Trade SIGNED (not broadcast)" in body
    assert "signed_tx_hash" in body
    assert "0xabcdef" in body
    assert "preflight_gas_used" in body


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_arb_bot_config_dry_run_signed_property() -> None:
    cfg_log = ArbBotConfig(demo=True, dry_run=True, dry_run_mode=_DRY_RUN_MODE_LOG)
    cfg_signed = ArbBotConfig(demo=True, dry_run=True, dry_run_mode=_DRY_RUN_MODE_SIGNED)
    cfg_off = ArbBotConfig(demo=True, dry_run=False, dry_run_mode=_DRY_RUN_MODE_SIGNED)

    assert cfg_log.dry_run_signed is False
    assert cfg_signed.dry_run_signed is True
    # No dry-run flag at all -> signed mode is inert.
    assert cfg_off.dry_run_signed is False


def test_arb_bot_config_rejects_unknown_dry_run_mode() -> None:
    with pytest.raises(ValueError, match="dry_run_mode"):
        ArbBotConfig(dry_run_mode="invalid")


# ---------------------------------------------------------------------------
# Executor wiring
# ---------------------------------------------------------------------------


def test_executor_dex_dry_run_signed_calls_live_leg_with_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``dex_dry_run_signed`` is set, ``_execute_dex_leg`` must invoke
    ``sync_execute_live_dex_leg`` with ``dry_run=True`` and never broadcast."""
    captured: dict[str, Any] = {}

    def fake_live_leg(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "success": True,
            "price": Decimal("3460.40"),
            "filled": Decimal("0.05"),
            "tx_hash": f"{DRY_RUN_TX_HASH_PREFIX}deadbeef",
            "dry_run": True,
            "signed_raw_tx_hex": "0x02f8aa0185...",
            "signed_tx_hash": "0xrealhash",
            "preflight_gas_used": 152_345,
        }

    import executor.live_dex_leg as live_dex_module

    monkeypatch.setattr(live_dex_module, "sync_execute_live_dex_leg", fake_live_leg)

    cfg = ExecutorConfig(simulation_mode=True, dex_dry_run_signed=True)
    executor = Executor(
        exchange_client=MagicMock(),
        pricing_module=MagicMock(),
        inventory_tracker=MagicMock(),
        config=cfg,
        dex_wallet=MagicMock(),
        dex_token_resolver=MagicMock(),
    )

    result = asyncio.run(executor._execute_dex_leg(_signal(), Decimal("0.05")))
    assert captured["dry_run"] is True
    assert result["success"] is True
    assert result["tx_hash"].startswith(DRY_RUN_TX_HASH_PREFIX)
    assert result["dry_run"] is True


def test_executor_metadata_propagates_signed_payload() -> None:
    ctx = ExecutionContext(signal=_signal())
    leg_result = {
        "success": True,
        "price": Decimal("3460"),
        "filled": Decimal("0.05"),
        "tx_hash": f"{DRY_RUN_TX_HASH_PREFIX}beef",
        "dry_run": True,
        "signed_raw_tx_hex": "0xdeadbeef",
        "signed_tx_hash": "0xrealhash",
        "preflight_gas_used": 200_000,
    }
    Executor._absorb_leg_metadata(Executor.__new__(Executor), ctx, leg_result, leg_label="leg2")
    assert ctx.metadata["leg2_dry_run"] is True
    assert ctx.metadata["leg2_signed_raw_tx_hex"] == "0xdeadbeef"
    assert ctx.metadata["leg2_signed_tx_hash"] == "0xrealhash"
    assert ctx.metadata["leg2_preflight_gas_used"] == 200_000


# ---------------------------------------------------------------------------
# Bot dry-run-log path still short-circuits (regression)
# ---------------------------------------------------------------------------


def test_dry_run_log_mode_never_calls_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MAX_TRADE_USD", "100000")
    cfg = ArbBotConfig(
        demo=True, dry_run=True, dry_run_mode=_DRY_RUN_MODE_LOG, min_score=Decimal("0")
    )
    bot = ArbBot(cfg)
    mock_ex = AsyncMock()

    async def _fail(*_a: Any, **_k: Any) -> None:
        raise AssertionError("execute must not be called in log dry-run mode")

    mock_ex.side_effect = _fail
    bot.executor.execute = mock_ex
    asyncio.run(bot._tick())
    mock_ex.assert_not_awaited()


# ---------------------------------------------------------------------------
# CEX virtual balances
# ---------------------------------------------------------------------------


def test_apply_cex_virtual_overrides_replaces_assets_and_keeps_others() -> None:
    real = {
        "BTC": {"free": Decimal("0.5"), "locked": Decimal("0"), "total": Decimal("0.5")},
        "ETH": {"free": Decimal("0.01"), "locked": Decimal("0"), "total": Decimal("0.01")},
        "info": {"unused": "ccxt-meta"},
    }
    out = _apply_cex_virtual_overrides(real, {"USDC": Decimal("5000"), "ETH": Decimal("2")})
    assert out["BTC"]["free"] == Decimal("0.5"), "untouched assets must be preserved"
    assert out["ETH"] == {
        "free": Decimal("2"),
        "locked": Decimal("0"),
        "used": Decimal("0"),
        "total": Decimal("2"),
    }
    assert out["USDC"] == {
        "free": Decimal("5000"),
        "locked": Decimal("0"),
        "used": Decimal("0"),
        "total": Decimal("5000"),
    }
    # CCXT meta keys (info, free, used, total maps) are passed through.
    assert out["info"] == {"unused": "ccxt-meta"}


def test_apply_cex_virtual_overrides_works_on_empty_real_dict() -> None:
    out = _apply_cex_virtual_overrides({}, {"USDC": Decimal("100")})
    assert out == {
        "USDC": {
            "free": Decimal("100"),
            "locked": Decimal("0"),
            "used": Decimal("0"),
            "total": Decimal("100"),
        },
    }


def test_apply_cex_virtual_overrides_handles_non_dict_real() -> None:
    out = _apply_cex_virtual_overrides("not a dict", {"USDC": Decimal("1")})
    assert "USDC" in out


def test_sync_balances_applies_cex_override_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_sync_balances` must merge ARB_VIRTUAL_CEX_BALANCES into the inventory."""
    monkeypatch.setenv("ARB_MAX_TRADE_USD", "100000")
    monkeypatch.setenv("ARB_VIRTUAL_CEX_BALANCES", "USDC=5000,ETH=2")
    cfg = ArbBotConfig(demo=True, dry_run=True, dry_run_mode=_DRY_RUN_MODE_SIGNED)
    bot = ArbBot(cfg)
    bot.config.demo = False  # take the real CEX path inside _sync_balances
    captured: dict[str, Any] = {}

    real_dict = {
        "BTC": {"free": Decimal("0.1"), "locked": Decimal("0"), "total": Decimal("0.1")},
    }

    def fake_fetch_balance() -> dict[str, Any]:
        return real_dict

    bot.exchange = MagicMock()
    bot.exchange.fetch_balance = fake_fetch_balance

    def capture_update(_venue: Any, balances: dict[str, Any]) -> None:
        captured["balances"] = balances

    bot.inventory.update_from_cex = capture_update  # type: ignore[assignment]
    bot._fetch_wallet_balances = MagicMock(return_value={})  # noqa: SLF001

    asyncio.run(bot._sync_balances())

    merged = captured["balances"]
    assert merged["BTC"]["free"] == Decimal("0.1"), "real assets remain"
    assert merged["USDC"]["free"] == Decimal("5000")
    assert merged["ETH"]["free"] == Decimal("2")


def test_sync_balances_seeds_inventory_when_cex_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the real CEX call raises but overrides are set, inventory still updates."""
    monkeypatch.setenv("ARB_MAX_TRADE_USD", "100000")
    monkeypatch.setenv("ARB_VIRTUAL_CEX_BALANCES", "USDC=2500")
    cfg = ArbBotConfig(demo=True, dry_run=True, dry_run_mode=_DRY_RUN_MODE_SIGNED)
    bot = ArbBot(cfg)
    bot.config.demo = False

    bot.exchange = MagicMock()
    bot.exchange.fetch_balance = MagicMock(side_effect=RuntimeError("ccxt offline"))
    captured: dict[str, Any] = {}

    def capture_update(_venue: Any, balances: dict[str, Any]) -> None:
        captured["balances"] = balances

    bot.inventory.update_from_cex = capture_update  # type: ignore[assignment]
    bot._fetch_wallet_balances = MagicMock(return_value={})  # noqa: SLF001

    asyncio.run(bot._sync_balances())
    assert captured["balances"]["USDC"]["free"] == Decimal("2500")
    # cex_connected flips back to True once an override fills the gap so the
    # per-tick reason stops blaming a missing CEX.
    assert bot.health.cex_connected is True


def test_sync_balances_ignores_cex_override_when_not_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARB_MAX_TRADE_USD", "100000")
    monkeypatch.setenv("ARB_VIRTUAL_CEX_BALANCES", "USDC=99999")
    # dry_run=False -> override must not leak into live inventory.
    cfg = ArbBotConfig(demo=True, dry_run=False, simulation=True)
    bot = ArbBot(cfg)
    bot.config.demo = False

    bot.exchange = MagicMock()
    bot.exchange.fetch_balance = MagicMock(
        return_value={
            "USDC": {"free": Decimal("10"), "locked": Decimal("0"), "total": Decimal("10")},
        },
    )
    captured: dict[str, Any] = {}

    def capture_update(_venue: Any, balances: dict[str, Any]) -> None:
        captured["balances"] = balances

    bot.inventory.update_from_cex = capture_update  # type: ignore[assignment]
    bot._fetch_wallet_balances = MagicMock(return_value={})  # noqa: SLF001

    asyncio.run(bot._sync_balances())
    assert captured["balances"]["USDC"]["free"] == Decimal("10"), "live balances untouched"
