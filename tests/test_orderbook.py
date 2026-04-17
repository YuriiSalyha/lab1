"""Tests for exchange.orderbook.OrderBookAnalyzer."""

from __future__ import annotations

from decimal import Decimal

from exchange.orderbook import OrderBookAnalyzer, _d


def _ob(
    bids: list[tuple[Decimal, Decimal]],
    asks: list[tuple[Decimal, Decimal]],
    *,
    mid: Decimal | None = None,
    spread_bps: Decimal | None = None,
) -> dict:
    bb = (bids[0][0], bids[0][1]) if bids else None
    ba = (asks[0][0], asks[0][1]) if asks else None
    if mid is None and bb and ba:
        mid = (bb[0] + ba[0]) / Decimal("2")
    ob: dict = {
        "symbol": "ETH/USDT",
        "timestamp": 0,
        "bids": bids,
        "asks": asks,
        "best_bid": bb,
        "best_ask": ba,
        "mid_price": mid,
        "spread_bps": spread_bps,
    }
    return ob


def test_levels_consumed_counts_only_full_levels():
    """A level increments the counter only when its entire size is taken."""
    ob = _ob(
        bids=[(Decimal("100"), Decimal("50"))],
        asks=[(Decimal("101"), Decimal("10"))],
    )
    full = OrderBookAnalyzer(ob).walk_the_book("buy", 10.0)
    assert full["levels_consumed"] == 1
    partial = OrderBookAnalyzer(ob).walk_the_book("buy", 2.0)
    assert partial["levels_consumed"] == 0


def test_walk_the_book_exact_fill():
    """Fill exactly at one price level."""
    ob = _ob(
        bids=[(Decimal("100"), Decimal("50"))],
        asks=[(Decimal("101"), Decimal("10"))],
    )
    a = OrderBookAnalyzer(ob)
    r = a.walk_the_book("buy", 2.0)
    assert r["fully_filled"] is True
    # Partial take from the only level — level not fully exhausted.
    assert r["levels_consumed"] == 0
    assert len(r["fills"]) == 1
    assert r["fills"][0]["price"] == Decimal("101")
    assert r["fills"][0]["qty"] == Decimal("2")
    assert r["avg_price"] == Decimal("101")
    assert r["total_cost"] == Decimal("202")


def test_walk_the_book_multiple_levels():
    """Fill across multiple price levels, avg price correct."""
    ob = _ob(
        bids=[(Decimal("100"), Decimal("1"))],
        asks=[
            (Decimal("101"), Decimal("1")),
            (Decimal("102"), Decimal("2")),
        ],
    )
    a = OrderBookAnalyzer(ob)
    r = a.walk_the_book("buy", 2.5)
    assert r["fully_filled"] is True
    # First level fully taken (1/1); second level partial (1.5/2).
    assert r["levels_consumed"] == 1
    # 1 @ 101 + 1.5 @ 102 = 101 + 153 = 254; / 2.5 = 101.6
    assert r["avg_price"] == Decimal("254") / Decimal("2.5")
    assert r["total_cost"] == Decimal("101") + Decimal("102") * Decimal("1.5")


def test_walk_the_book_insufficient_liquidity():
    """Returns fully_filled=False when book is too thin."""
    ob = _ob(
        bids=[(Decimal("100"), Decimal("1"))],
        asks=[
            (Decimal("101"), Decimal("1")),
            (Decimal("102"), Decimal("1")),
        ],
    )
    a = OrderBookAnalyzer(ob)
    r = a.walk_the_book("buy", 5.0)
    assert r["fully_filled"] is False
    assert sum(f["qty"] for f in r["fills"]) == Decimal("2")
    assert r["levels_consumed"] == 2  # both ask levels fully exhausted


def test_depth_at_bps_correct():
    """Depth at 10 bps matches manual calculation."""
    # Best bid 2000; floor = 2000 * (1 - 10/10000) = 1998
    bids = [
        (Decimal("2000"), Decimal("10")),
        (Decimal("1999"), Decimal("5")),
        (Decimal("1990"), Decimal("100")),
    ]
    asks = [(Decimal("2001"), Decimal("1"))]
    ob = _ob(bids=bids, asks=asks)
    a = OrderBookAnalyzer(ob)
    d = a.depth_at_bps("bid", 10)
    # 2000 and 1999 >= 1998; 1990 is not
    assert d == Decimal("15")

    asks2 = [
        (Decimal("2001"), Decimal("3")),
        (Decimal("2004"), Decimal("2")),
    ]
    ob2 = _ob(bids=[(Decimal("2000"), Decimal("1"))], asks=asks2)
    a2 = OrderBookAnalyzer(ob2)
    d_ask = a2.depth_at_bps("ask", 10)
    best_ask = Decimal("2001")
    ceil = best_ask * (Decimal("1") + Decimal("10") / Decimal("10000"))
    assert Decimal("2004") > ceil
    assert d_ask == Decimal("3")


def test_imbalance_range():
    """Imbalance always in [-1.0, +1.0]."""
    cases = [
        _ob(
            bids=[(Decimal("1"), Decimal("100"))],
            asks=[(Decimal("2"), Decimal("1"))],
        ),
        _ob(
            bids=[(Decimal("1"), Decimal("1"))],
            asks=[(Decimal("2"), Decimal("100"))],
        ),
        _ob(
            bids=[(Decimal("1"), Decimal("10")), (Decimal("0.9"), Decimal("10"))],
            asks=[(Decimal("1.1"), Decimal("10")), (Decimal("1.2"), Decimal("10"))],
        ),
    ]
    for ob in cases:
        x = OrderBookAnalyzer(ob).imbalance(levels=10)
        assert -1.0 <= x <= 1.0


def test_effective_spread_greater_than_quoted():
    """Effective spread >= quoted spread for any qty > 0 (same mid definition)."""
    ob = _ob(
        bids=[
            (Decimal("100"), Decimal("10")),
            (Decimal("99"), Decimal("10")),
        ],
        asks=[
            (Decimal("101"), Decimal("10")),
            (Decimal("102"), Decimal("10")),
        ],
        spread_bps=None,
    )
    a = OrderBookAnalyzer(ob)
    q = _d(a.quoted_spread_bps())
    for qty in (0.01, 1.0, 15.0):
        e = a.effective_spread(qty)
        assert e >= q - Decimal("1e-9")


def test_walk_sell_slippage_positive_when_eating_bids():
    ob = _ob(
        bids=[
            (Decimal("100"), Decimal("1")),
            (Decimal("99"), Decimal("10")),
        ],
        asks=[(Decimal("101"), Decimal("10"))],
    )
    r = OrderBookAnalyzer(ob).walk_the_book("sell", 5.0)
    assert r["slippage_bps"] > 0
    assert r["avg_price"] < Decimal("100")
