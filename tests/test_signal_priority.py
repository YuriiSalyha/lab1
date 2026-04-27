"""Tests for :mod:`strategy.signal_priority`."""

from __future__ import annotations

import time
from decimal import Decimal

from strategy.signal import Direction, Signal
from strategy.signal_priority import ScoredCandidate, sort_candidates_by_priority


def _sig(
    *,
    score: Decimal,
    net: Decimal,
    spread: Decimal,
    pair: str = "ETH/USDT",
) -> Signal:
    now = time.time()
    return Signal(
        signal_id=f"S_{pair}_{score}",
        pair=pair,
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=Decimal("2000"),
        dex_price=Decimal("2010"),
        spread_bps=spread,
        size=Decimal("0.1"),
        expected_gross_pnl=Decimal("1"),
        expected_fees=Decimal("0"),
        expected_net_pnl=net,
        score=score,
        timestamp=now,
        expiry=now + 10,
        inventory_ok=True,
        within_limits=True,
    )


def test_sort_empty():
    assert sort_candidates_by_priority([]) == []


def test_sort_by_score_then_net_then_spread_then_pair():
    a = ScoredCandidate(
        signal=_sig(score=Decimal("70"), net=Decimal("1"), spread=Decimal("50")),
        pair="A/A",
    )
    b = ScoredCandidate(
        signal=_sig(score=Decimal("90"), net=Decimal("1"), spread=Decimal("50")),
        pair="B/B",
    )
    c = ScoredCandidate(
        signal=_sig(score=Decimal("90"), net=Decimal("2"), spread=Decimal("50")),
        pair="C/C",
    )
    d = ScoredCandidate(
        signal=_sig(score=Decimal("90"), net=Decimal("2"), spread=Decimal("60")),
        pair="D/D",
    )
    e = ScoredCandidate(
        signal=_sig(score=Decimal("90"), net=Decimal("2"), spread=Decimal("60")),
        pair="E/E",
    )
    out = sort_candidates_by_priority([a, b, c, d, e])
    # Higher score first; ties break on net, spread, then stable pair name.
    assert [x.pair for x in out] == ["D/D", "E/E", "C/C", "B/B", "A/A"]
