"""Rank tradable signals so higher-priority opportunities execute first."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from strategy.signal import Signal


@dataclass(frozen=True)
class ScoredCandidate:
    """One tradable signal with its source pair (for logging / scorer)."""

    signal: Signal
    pair: str

    def sort_key(self) -> tuple[Decimal, Decimal, Decimal, str]:
        """Descending priority: score, expected_net_pnl, spread_bps, then stable pair tie-break."""
        return (
            -self.signal.score,
            -self.signal.expected_net_pnl,
            -self.signal.spread_bps,
            self.pair,
        )


def sort_candidates_by_priority(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Return a new list sorted highest-priority first."""
    return sorted(candidates, key=lambda c: c.sort_key())
