"""Multi-factor signal scoring with TTL decay.

Scores are ``Decimal`` in ``[0, 100]``. Weights are ``Decimal`` so downstream
comparisons stay exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from strategy.signal import SCORE_MAX, SCORE_MIN, Signal, to_decimal

# How aggressively scores decay across their TTL (1.0 == reaches 0 at expiry, 0.5 == half).
DEFAULT_DECAY_STRENGTH = Decimal("0.5")
# Maximum history window for success-rate scoring.
HISTORY_MAX = 100
HISTORY_WINDOW = 20
# Minimum samples required before a pair's win-rate starts replacing the neutral score.
MIN_HISTORY_SAMPLES = 3
NEUTRAL_SCORE = Decimal("50")
INVENTORY_PENALTY_SCORE = Decimal("20")
INVENTORY_OK_SCORE = Decimal("60")
LIQUIDITY_PLACEHOLDER_SCORE = Decimal("80")


@dataclass
class ScorerConfig:
    spread_weight: Decimal = Decimal("0.4")
    liquidity_weight: Decimal = Decimal("0.2")
    inventory_weight: Decimal = Decimal("0.2")
    history_weight: Decimal = Decimal("0.2")
    excellent_spread_bps: Decimal = Decimal("100")
    min_spread_bps: Decimal = Decimal("30")
    decay_strength: Decimal = DEFAULT_DECAY_STRENGTH

    def __post_init__(self) -> None:
        self.spread_weight = to_decimal(self.spread_weight)
        self.liquidity_weight = to_decimal(self.liquidity_weight)
        self.inventory_weight = to_decimal(self.inventory_weight)
        self.history_weight = to_decimal(self.history_weight)
        self.excellent_spread_bps = to_decimal(self.excellent_spread_bps)
        self.min_spread_bps = to_decimal(self.min_spread_bps)
        self.decay_strength = to_decimal(self.decay_strength)
        if self.excellent_spread_bps <= self.min_spread_bps:
            raise ValueError("excellent_spread_bps must exceed min_spread_bps")
        total_weight = (
            self.spread_weight + self.liquidity_weight + self.inventory_weight + self.history_weight
        )
        if total_weight <= 0:
            raise ValueError("weights sum must be positive")


class SignalScorer:
    """Score signals 0-100 using spread, liquidity, inventory, and history."""

    def __init__(self, config: Optional[ScorerConfig] = None) -> None:
        self.config = config or ScorerConfig()
        self.recent_results: list[tuple[str, bool]] = []

    def score(self, signal: Signal, inventory_state: list[dict[str, Any]]) -> Decimal:
        """Return a 0-100 Decimal score."""
        cfg = self.config
        spread_s = self._score_spread(signal.spread_bps)
        liquidity_s = LIQUIDITY_PLACEHOLDER_SCORE
        inventory_s = self._score_inventory(signal, inventory_state)
        history_s = self._score_history(signal.pair)

        weighted = (
            spread_s * cfg.spread_weight
            + liquidity_s * cfg.liquidity_weight
            + inventory_s * cfg.inventory_weight
            + history_s * cfg.history_weight
        )
        clamped = max(SCORE_MIN, min(SCORE_MAX, weighted))
        return clamped.quantize(Decimal("0.1"))

    # ------------------------------------------------------------------
    # Individual factors
    # ------------------------------------------------------------------

    def _score_spread(self, spread_bps: Any) -> Decimal:
        s = to_decimal(spread_bps)
        cfg = self.config
        if s <= cfg.min_spread_bps:
            return SCORE_MIN
        if s >= cfg.excellent_spread_bps:
            return SCORE_MAX
        range_bps = cfg.excellent_spread_bps - cfg.min_spread_bps
        return (s - cfg.min_spread_bps) / range_bps * SCORE_MAX

    def _score_inventory(self, signal: Signal, skews: list[dict[str, Any]]) -> Decimal:
        """Low score if base asset needs rebalancing."""
        base = signal.pair.split("/")[0]
        relevant = [s for s in skews if s.get("asset") == base]
        if not signal.inventory_ok:
            return INVENTORY_PENALTY_SCORE
        if any(s.get("needs_rebalance") for s in relevant):
            return INVENTORY_PENALTY_SCORE
        return INVENTORY_OK_SCORE

    def _score_history(self, pair: str) -> Decimal:
        results = [r for p, r in self.recent_results[-HISTORY_WINDOW:] if p == pair]
        n = len(results)
        if n < MIN_HISTORY_SAMPLES:
            return NEUTRAL_SCORE
        wins = sum(1 for r in results if r)
        return Decimal(wins) / Decimal(n) * SCORE_MAX

    # ------------------------------------------------------------------
    # State mutation / decay
    # ------------------------------------------------------------------

    def record_result(self, pair: str, success: bool) -> None:
        """Track the outcome of a signal for future history scoring."""
        self.recent_results.append((pair, bool(success)))
        if len(self.recent_results) > HISTORY_MAX:
            self.recent_results = self.recent_results[-HISTORY_MAX:]

    def apply_decay(self, signal: Signal) -> Decimal:
        """Decay a signal's score based on how much of its TTL has elapsed.

        Safe for signals with zero or negative TTL (returns 0 — these are
        already expired and should not trade).
        """
        ttl = to_decimal(signal.ttl_seconds())
        if ttl <= 0:
            return SCORE_MIN
        age = to_decimal(signal.age_seconds())
        if age <= 0:
            return to_decimal(signal.score)
        factor = Decimal("1") - (age / ttl) * self.config.decay_strength
        if factor < 0:
            factor = Decimal("0")
        return (to_decimal(signal.score) * factor).quantize(Decimal("0.1"))
