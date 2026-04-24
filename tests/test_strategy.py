"""Adversarial tests for strategy: SignalGenerator + SignalScorer + fees.

All tests use stdlib mocks; no network, no real exchange.
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from inventory.tracker import InventoryTracker, Venue
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import (
    INVENTORY_OK_SCORE,
    INVENTORY_PENALTY_SCORE,
    NEUTRAL_SCORE,
    ScorerConfig,
    SignalScorer,
)
from strategy.signal import Direction, Signal, to_decimal

# --- Fixtures ----------------------------------------------------------------


def _book(bid: Decimal, ask: Decimal) -> dict:
    return {
        "symbol": "ETH/USDT",
        "timestamp": 0,
        "bids": [(bid, Decimal("10"))],
        "asks": [(ask, Decimal("10"))],
        "best_bid": (bid, Decimal("10")),
        "best_ask": (ask, Decimal("10")),
        "mid_price": (bid + ask) / Decimal("2"),
        "spread_bps": (ask - bid) / ((bid + ask) / Decimal("2")) * Decimal("10000"),
    }


@pytest.fixture
def mock_exchange() -> MagicMock:
    ex = MagicMock()
    ex.fetch_order_book.return_value = _book(Decimal("2000"), Decimal("2001"))
    return ex


@pytest.fixture
def tracker() -> InventoryTracker:
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": Decimal("10"), "locked": Decimal("0")},
            "USDT": {"free": Decimal("100000"), "locked": Decimal("0")},
        },
    )
    t.update_from_wallet(
        Venue.WALLET,
        {"ETH": Decimal("10"), "USDT": Decimal("100000")},
    )
    return t


@pytest.fixture
def fees() -> FeeStructure:
    # Zero gas keeps fees additive-only so tiny test notionals stay profitable.
    return FeeStructure(
        cex_taker_bps=Decimal("10"),
        dex_swap_bps=Decimal("30"),
        gas_cost_usd=Decimal("0"),
    )


@pytest.fixture
def generator(mock_exchange, tracker, fees) -> SignalGenerator:
    return SignalGenerator(
        mock_exchange,
        None,  # no pricing module -> stub DEX prices
        tracker,
        fees,
        {
            "min_spread_bps": Decimal("50"),
            "min_profit_usd": Decimal("0.1"),
            "cooldown_seconds": 2.0,
        },
    )


# --- Signal generator --------------------------------------------------------


def test_generate_signal_profitable(generator):
    """Stub DEX is 0.8% above mid, so the spread always beats 50 bps."""
    signal = generator.generate("ETH/USDT", Decimal("0.1"))
    assert signal is not None
    assert signal.spread_bps > Decimal("50")
    assert signal.expected_net_pnl > 0
    assert signal.direction == Direction.BUY_CEX_SELL_DEX
    assert isinstance(signal.cex_price, Decimal)
    assert isinstance(signal.expected_net_pnl, Decimal)


def test_generate_signal_no_opportunity(mock_exchange, tracker, fees):
    """Minimum spread set very high -> generator returns None."""
    gen = SignalGenerator(
        mock_exchange,
        None,
        tracker,
        fees,
        {"min_spread_bps": Decimal("10000"), "cooldown_seconds": 0},
    )
    assert gen.generate("ETH/USDT", Decimal("0.1")) is None


def test_cooldown_prevents_rapid_signals(generator, monkeypatch):
    """Second signal within cooldown window returns None; later signal OK again."""
    t = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    assert generator.generate("ETH/USDT", Decimal("0.1")) is not None
    # Still within cooldown (default 2s, we advance 1s).
    t["now"] = 1_001.0
    assert generator.generate("ETH/USDT", Decimal("0.1")) is None
    # After cooldown expires, we get a signal again.
    t["now"] = 1_010.0
    assert generator.generate("ETH/USDT", Decimal("0.1")) is not None


def test_direction_selection(generator, monkeypatch):
    """Prices crafted so BUY_DEX_SELL_CEX wins (CEX bid >> DEX buy)."""

    def fake_prices(pair, size):
        # cex_bid 2200, cex_ask 2100, dex_buy/sell 2000 -> spread_b dominates.
        return {
            "cex_bid": Decimal("2200"),
            "cex_ask": Decimal("2100"),
            "dex_buy": Decimal("2000"),
            "dex_sell": Decimal("2000"),
        }

    monkeypatch.setattr(generator, "_fetch_prices", fake_prices)
    signal = generator.generate("ETH/USDT", Decimal("0.1"))
    assert signal is not None
    assert signal.direction == Direction.BUY_DEX_SELL_CEX


def test_inventory_insufficient_sets_flag(mock_exchange, fees):
    """Empty inventory -> signal flagged inventory_ok=False (still emitted)."""
    empty = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    gen = SignalGenerator(
        mock_exchange,
        None,
        empty,
        fees,
        {"min_spread_bps": Decimal("50"), "min_profit_usd": Decimal("0"), "cooldown_seconds": 0},
    )
    signal = gen.generate("ETH/USDT", Decimal("0.1"))
    assert signal is not None
    assert signal.inventory_ok is False


def test_generator_rejects_non_positive_size(generator):
    with pytest.raises(ValueError):
        generator.generate("ETH/USDT", Decimal("0"))
    with pytest.raises(ValueError):
        generator.generate("ETH/USDT", Decimal("-1"))


def test_generator_rejects_malformed_pair(generator):
    with pytest.raises(ValueError):
        generator.generate("ETHUSDT", Decimal("0.1"))


def test_signal_negative_size_rejected():
    """Signal dataclass guards against impossible sizes at construction."""
    with pytest.raises(ValueError):
        Signal(
            signal_id="x",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=Decimal("100"),
            dex_price=Decimal("101"),
            spread_bps=Decimal("100"),
            size=Decimal("-1"),
            expected_gross_pnl=Decimal("0"),
            expected_fees=Decimal("0"),
            expected_net_pnl=Decimal("0"),
            score=Decimal("0"),
            timestamp=time.time(),
            expiry=time.time() + 1,
            inventory_ok=True,
            within_limits=True,
        )


def test_signal_negative_price_rejected():
    with pytest.raises(ValueError):
        Signal(
            signal_id="x",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=Decimal("-5"),
            dex_price=Decimal("101"),
            spread_bps=Decimal("100"),
            size=Decimal("1"),
            expected_gross_pnl=Decimal("0"),
            expected_fees=Decimal("0"),
            expected_net_pnl=Decimal("0"),
            score=Decimal("0"),
            timestamp=time.time(),
            expiry=time.time() + 1,
            inventory_ok=True,
            within_limits=True,
        )


def test_signal_score_clamped_to_100():
    s = _build_signal(score=Decimal("1000"))
    assert s.score == Decimal("100")


def test_feestructure_raises_on_zero_notional(fees):
    with pytest.raises(ValueError):
        fees.total_fee_bps(Decimal("0"))
    with pytest.raises(ValueError):
        fees.net_profit_usd(Decimal("100"), Decimal("0"))


def test_feestructure_returns_decimal(fees):
    assert isinstance(fees.total_fee_bps(Decimal("10000")), Decimal)
    assert isinstance(fees.net_profit_usd(Decimal("50"), Decimal("10000")), Decimal)


# --- Signal scorer -----------------------------------------------------------


def _build_signal(
    pair: str = "ETH/USDT",
    spread_bps: Decimal = Decimal("60"),
    inventory_ok: bool = True,
    timestamp: float | None = None,
    ttl: float = 5.0,
    score: Decimal = Decimal("0"),
) -> Signal:
    now = timestamp if timestamp is not None else time.time()
    return Signal(
        signal_id="id",
        pair=pair,
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("2000"),
        dex_price=Decimal("2010"),
        spread_bps=spread_bps,
        size=Decimal("0.1"),
        expected_gross_pnl=Decimal("1"),
        expected_fees=Decimal("0.1"),
        expected_net_pnl=Decimal("0.9"),
        score=score,
        timestamp=now,
        expiry=now + ttl,
        inventory_ok=inventory_ok,
        within_limits=True,
    )


def test_score_high_spread():
    scorer = SignalScorer()
    sig = _build_signal(spread_bps=Decimal("100"))
    assert scorer.score(sig, []) >= Decimal("60")


def test_score_below_min_returns_zero_spread_component():
    """Under the minimum spread, spread-component is zero; other factors still contribute."""
    scorer = SignalScorer()
    sig = _build_signal(spread_bps=Decimal("10"))
    out = scorer.score(sig, [])
    # liquidity placeholder (80) + inventory (60) + history (50) all non-zero -> still > 0
    # but significantly below a 100-bps scenario.
    assert out < Decimal("60")


def test_score_inventory_penalty():
    scorer = SignalScorer()
    sig = _build_signal(spread_bps=Decimal("100"))
    skews = [{"asset": "ETH", "needs_rebalance": True, "max_deviation_pct": 60}]
    out = scorer.score(sig, skews)
    # penalty component used => final weighted score should drop.
    assert out < Decimal("80")
    assert out > Decimal("0")


def test_score_inventory_ok_when_inventory_flag_true():
    scorer = SignalScorer()
    sig = _build_signal()
    assert scorer._score_inventory(sig, []) == INVENTORY_OK_SCORE


def test_score_inventory_penalty_when_flag_false():
    scorer = SignalScorer()
    sig = _build_signal(inventory_ok=False)
    assert scorer._score_inventory(sig, []) == INVENTORY_PENALTY_SCORE


def test_decay_over_time(monkeypatch):
    scorer = SignalScorer()
    now = 1_000.0
    sig = _build_signal(timestamp=now, ttl=10.0, score=Decimal("80"))
    # Pretend 8 seconds have elapsed -> 80% of TTL
    monkeypatch.setattr(time, "time", lambda: now + 8.0)
    decayed = scorer.apply_decay(sig)
    assert decayed < Decimal("80")
    assert decayed > Decimal("0")


def test_decay_with_zero_ttl_safe():
    """Expired/zero-TTL signals decay to 0 without ZeroDivisionError."""
    scorer = SignalScorer()
    sig = _build_signal(ttl=0.0, score=Decimal("80"))
    # Since expiry == timestamp, apply_decay returns 0.
    assert scorer.apply_decay(sig) == Decimal("0")


def test_history_defaults_when_few_samples():
    scorer = SignalScorer()
    assert scorer._score_history("ETH/USDT") == NEUTRAL_SCORE
    scorer.record_result("ETH/USDT", True)
    assert scorer._score_history("ETH/USDT") == NEUTRAL_SCORE  # still <3 samples


def test_history_window_caps_memory():
    """record_result caps memory regardless of call count."""
    scorer = SignalScorer()
    for _ in range(500):
        scorer.record_result("ETH/USDT", True)
    assert len(scorer.recent_results) == 100


def test_history_uses_recent_samples_only():
    """Old wins don't mask new losses: only last HISTORY_WINDOW count."""
    scorer = SignalScorer()
    for _ in range(30):
        scorer.record_result("ETH/USDT", True)
    for _ in range(20):
        scorer.record_result("ETH/USDT", False)
    assert scorer._score_history("ETH/USDT") == Decimal("0")


def test_scorer_config_rejects_invalid_ranges():
    with pytest.raises(ValueError):
        ScorerConfig(excellent_spread_bps=Decimal("10"), min_spread_bps=Decimal("20"))


def test_to_decimal_handles_str_and_none():
    assert to_decimal(None) == Decimal("0")
    assert to_decimal("1.5") == Decimal("1.5")
    # float path routes via str() to avoid binary-float noise.
    assert to_decimal(0.1) == Decimal("0.1")


def test_signalgenerator_fetch_failure_triggers_cooldown(generator, mock_exchange, monkeypatch):
    """When fetch_order_book raises, generator should still treat it as a cooldown event."""
    mock_exchange.fetch_order_book.side_effect = RuntimeError("offline")
    t = {"now": 5_000.0}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    assert generator.generate("ETH/USDT", Decimal("0.1")) is None
    # Immediate retry is still suppressed by cooldown.
    mock_exchange.fetch_order_book.side_effect = None
    mock_exchange.fetch_order_book.return_value = _book(Decimal("2000"), Decimal("2001"))
    assert generator.generate("ETH/USDT", Decimal("0.1")) is None
    # After cooldown, we succeed.
    t["now"] = 5_100.0
    assert generator.generate("ETH/USDT", Decimal("0.1")) is not None


def test_generate_within_limits_flag(mock_exchange, tracker, fees):
    """Size pushing notional past max_position_usd flips within_limits flag."""
    gen = SignalGenerator(
        mock_exchange,
        None,
        tracker,
        fees,
        {
            "min_spread_bps": Decimal("50"),
            "min_profit_usd": Decimal("0"),
            "max_position_usd": Decimal("10"),
            "cooldown_seconds": 0,
        },
    )
    sig = gen.generate("ETH/USDT", Decimal("0.1"))
    assert sig is not None
    assert sig.within_limits is False
    assert sig.is_valid() is False
