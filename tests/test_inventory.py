"""Unit tests for inventory tracker, rebalancer, and PnL."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.rebalancer import MIN_OPERATING_BALANCE, TRANSFER_FEES, RebalancePlanner
from inventory.tracker import InventoryTracker, Venue


@pytest.fixture
def venues():
    return [Venue.BINANCE, Venue.WALLET]


@pytest.fixture
def tracker(venues):
    return InventoryTracker(venues)


def _cex_bal(asset: str, free: str, locked: str = "0") -> dict:
    free_d, locked_d = Decimal(free), Decimal(locked)
    return {asset: {"free": free_d, "locked": locked_d, "total": free_d + locked_d}}


# --- InventoryTracker ---


def test_snapshot_aggregates_across_venues(tracker):
    """Total ETH = Binance ETH + Wallet ETH."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "12.5"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("7.5")})
    snap = tracker.snapshot()
    assert snap["totals"]["ETH"] == Decimal("20")


def test_can_execute_passes_when_sufficient(tracker):
    """Returns can_execute=True with enough balance on both sides."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("USDT", "10000"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
    r = tracker.can_execute(
        Venue.BINANCE,
        "USDT",
        Decimal("100"),
        Venue.WALLET,
        "ETH",
        Decimal("0.03"),
    )
    assert r["can_execute"] is True
    assert r["reason"] is None


def test_can_execute_fails_insufficient_buy(tracker):
    """Returns can_execute=False when buy venue lacks funds."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("USDT", "10"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
    r = tracker.can_execute(
        Venue.BINANCE,
        "USDT",
        Decimal("100"),
        Venue.WALLET,
        "ETH",
        Decimal("0.01"),
    )
    assert r["can_execute"] is False
    assert "USDT" in r["reason"]


def test_can_execute_fails_insufficient_sell(tracker):
    """Returns can_execute=False when sell venue lacks asset."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("USDT", "10000"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("0.001")})
    r = tracker.can_execute(
        Venue.BINANCE,
        "USDT",
        Decimal("100"),
        Venue.WALLET,
        "ETH",
        Decimal("1"),
    )
    assert r["can_execute"] is False
    assert "ETH" in r["reason"]


def test_record_trade_updates_balances(tracker):
    """After buy trade: base increases, quote decreases, fee deducted."""
    tracker.update_from_cex(
        Venue.BINANCE,
        {**_cex_bal("ETH", "1"), **_cex_bal("USDT", "3000")},
    )
    tracker.record_trade(
        Venue.BINANCE,
        "buy",
        "ETH",
        "USDT",
        Decimal("0.5"),
        Decimal("1000"),
        Decimal("1"),
        "USDT",
    )
    assert tracker.get_available(Venue.BINANCE, "ETH") == Decimal("1.5")
    assert tracker.get_available(Venue.BINANCE, "USDT") == Decimal("1999")
    tracker.record_trade(
        Venue.BINANCE,
        "sell",
        "ETH",
        "USDT",
        Decimal("0.2"),
        Decimal("500"),
        Decimal("0.0001"),
        "ETH",
    )
    eth = tracker.get_available(Venue.BINANCE, "ETH")
    assert eth == Decimal("1.5") - Decimal("0.2") - Decimal("0.0001")


def test_skew_detects_imbalance(tracker):
    """80/20 split shows >30% deviation."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    s = tracker.skew("ETH")
    assert s["max_deviation_pct"] > 30
    assert s["needs_rebalance"] is True


def test_skew_balanced(tracker):
    """50/50 split shows ~0% deviation."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "5"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
    s = tracker.skew("ETH")
    assert s["max_deviation_pct"] < 1e-6
    assert s["needs_rebalance"] is False


def test_skew_moderate_60_40_no_rebalance(tracker):
    """60/40 vs 50/50 — deviation below default 30% rebalance threshold."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "6"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("4")})
    s = tracker.skew("ETH")
    assert s["max_deviation_pct"] < 30
    assert s["needs_rebalance"] is False


def test_skew_extreme_90_10_needs_rebalance(tracker):
    """90/10 split exceeds threshold."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "1"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("9")})
    s = tracker.skew("ETH")
    assert s["needs_rebalance"] is True


def test_get_skews_returns_all_assets(tracker):
    """get_skews() returns one entry per tracked asset."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "1"))
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("USDT", "100"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("1"), "USDT": Decimal("100")})
    sk = tracker.get_skews()
    assets = {row["asset"] for row in sk}
    assert assets == {"ETH", "USDT"}


# --- RebalancePlanner ---


def test_check_detects_skewed_asset(tracker):
    """Asset with 80/20 split flagged for rebalance."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    planner = RebalancePlanner(tracker)
    rows = planner.check_all()
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["needs_rebalance"] is True


def test_check_passes_balanced_asset(tracker):
    """Asset with 55/45 split not flagged."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "55"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("45")})
    planner = RebalancePlanner(tracker)
    rows = planner.check_all()
    eth = next(r for r in rows if r["asset"] == "ETH")
    assert eth["needs_rebalance"] is False


def test_plan_generates_correct_transfer(tracker):
    """Plan moves the right amount in the right direction."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    planner = RebalancePlanner(tracker)
    plans = planner.plan("ETH")
    assert len(plans) == 1
    p = plans[0]
    assert p.from_venue == Venue.WALLET
    assert p.to_venue == Venue.BINANCE
    fee = TRANSFER_FEES["ETH"]["withdrawal_fee"]
    assert p.amount == Decimal("5") - Decimal("2") + fee


def test_plan_respects_min_operating_balance(tracker, monkeypatch):
    """Never plans transfer that leaves venue below minimum."""
    monkeypatch.setitem(MIN_OPERATING_BALANCE, "ETH", Decimal("7.99"))
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    planner = RebalancePlanner(tracker)
    assert planner.plan("ETH") == []


def test_plan_accounts_for_fees(tracker):
    """Net amount received = amount - fee."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    planner = RebalancePlanner(tracker)
    p = planner.plan("ETH")[0]
    fee = TRANSFER_FEES["ETH"]["withdrawal_fee"]
    assert p.net_amount == p.amount - fee


def test_plan_empty_when_balanced(tracker):
    """No plans generated for balanced assets."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "5"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
    planner = RebalancePlanner(tracker)
    assert planner.plan("ETH") == []


def test_estimate_cost_sums_correctly(tracker):
    """Total fees and time calculated correctly."""
    tracker.update_from_cex(Venue.BINANCE, _cex_bal("ETH", "2"))
    tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})
    planner = RebalancePlanner(tracker)
    plans = planner.plan("ETH")
    c = planner.estimate_cost(plans)
    assert c["total_transfers"] == 1
    assert c["total_time_min"] == TRANSFER_FEES["ETH"]["estimated_time_min"]
    assert "ETH" in c["assets_affected"]
    assert c["total_fees_usd"] > 0


# --- PnL ---


def _leg(
    id_: str,
    venue: Venue,
    side: str,
    amount: str,
    price: str,
    fee: str,
    fee_asset: str,
) -> TradeLeg:
    return TradeLeg(
        id=id_,
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        venue=venue,
        symbol="ETH/USDT",
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_asset=fee_asset,
    )


def test_gross_pnl_calculation():
    """Gross PnL = sell revenue - buy cost."""
    buy = _leg("b", Venue.BINANCE, "buy", "1", "2000", "5", "USDT")
    sell = _leg("s", Venue.WALLET, "sell", "1", "2100", "5", "USDT")
    r = ArbRecord(
        id="a1",
        timestamp=buy.timestamp,
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("0"),
    )
    assert r.gross_pnl == Decimal("100")


def test_net_pnl_includes_all_fees():
    """Net PnL = gross - buy fee - sell fee - gas."""
    buy = _leg("b", Venue.BINANCE, "buy", "1", "2000", "10", "USDT")
    sell = _leg("s", Venue.WALLET, "sell", "1", "2100", "10", "USDT")
    r = ArbRecord(
        id="a1",
        timestamp=buy.timestamp,
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("2"),
    )
    assert r.net_pnl == r.gross_pnl - Decimal("22")  # 10 + 10 + 2 gas


def test_pnl_mixed_eth_and_usdt_fee_assets():
    """ETH fee leg priced in USD via REFERENCE_USD_PER_ETH; stables at 1:1."""
    buy = _leg("b", Venue.BINANCE, "buy", "1", "2000", "0.001", "ETH")
    sell = _leg("s", Venue.WALLET, "sell", "1", "2100", "3", "USDT")
    r = ArbRecord(
        id="mix",
        timestamp=buy.timestamp,
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("1"),
    )
    # 0.001 ETH * 2000 + 3 USDT + 1 gas
    assert r.total_fees == Decimal("2") + Decimal("3") + Decimal("1")


def test_pnl_bps_calculation():
    """PnL bps = net_pnl / notional * 10000."""
    buy = _leg("b", Venue.BINANCE, "buy", "2", "1000", "0", "USDT")
    sell = _leg("s", Venue.WALLET, "sell", "2", "1010", "0", "USDT")
    r = ArbRecord(
        id="a1",
        timestamp=buy.timestamp,
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("0"),
    )
    n = r.notional
    expected = r.net_pnl / n * Decimal("10000")
    assert r.net_pnl_bps == expected


def test_summary_win_rate():
    """Win rate = profitable trades / total trades."""
    eng = PnLEngine()
    for i, pnl in enumerate([Decimal("1"), Decimal("-1"), Decimal("3")]):
        buy = _leg(f"b{i}", Venue.BINANCE, "buy", "1", "1000", "0", "USDT")
        sell = _leg(f"s{i}", Venue.WALLET, "sell", "1", str(1000 + int(pnl)), "0", "USDT")
        eng.record(
            ArbRecord(
                id=f"t{i}",
                timestamp=buy.timestamp,
                buy_leg=buy,
                sell_leg=sell,
                gas_cost_usd=Decimal("0"),
            )
        )
    s = eng.summary()
    assert s["total_trades"] == 3
    assert abs(s["win_rate"] - (2 / 3)) < 1e-9


def test_summary_with_no_trades():
    """Summary returns zeros, no division errors."""
    s = PnLEngine().summary()
    assert s["total_trades"] == 0
    assert s["win_rate"] == 0.0
    assert s["total_pnl_usd"] == Decimal("0")


def test_export_csv_format(tmp_path: Path):
    """CSV has expected columns and correct values."""
    buy = _leg("b", Venue.BINANCE, "buy", "1", "2000", "1", "USDT")
    sell = _leg("s", Venue.WALLET, "sell", "1", "2010", "1", "USDT")
    r = ArbRecord(
        id="arb1",
        timestamp=datetime(2026, 6, 1, 15, 30, 0, tzinfo=timezone.utc),
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("0.5"),
    )
    eng = PnLEngine()
    eng.record(r)
    path = tmp_path / "out.csv"
    eng.export_csv(str(path))
    text = path.read_text(encoding="utf-8")
    assert "gross_pnl_usd" in text
    assert "arb1" in text
    assert "binance" in text
    assert str(r.net_pnl) in text.replace(",", "")
