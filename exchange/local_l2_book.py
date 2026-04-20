"""
Local L2 order book: snapshot + incremental deltas, normalized dict for :class:`OrderBookAnalyzer`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

SIDE_BID = "bid"
SIDE_ASK = "ask"


def _d(x: object) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class LocalL2Book:
    """
    In-memory price levels (bids high→low, asks low→high when exported).
    Quantity <= 0 removes a level.
    """

    def __init__(self) -> None:
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._sequence_id: int | None = None

    @property
    def sequence_id(self) -> int | None:
        return self._sequence_id

    def apply_snapshot(
        self,
        bids: list[tuple[object, object]],
        asks: list[tuple[object, object]],
        *,
        sequence_id: int | None = None,
    ) -> None:
        """Replace both sides."""
        self._bids = {}
        self._asks = {}
        for p, q in bids:
            price, qty = _d(p), _d(q)
            if qty > 0:
                self._bids[price] = qty
        for p, q in asks:
            price, qty = _d(p), _d(q)
            if qty > 0:
                self._asks[price] = qty
        self._sequence_id = sequence_id

    def apply_delta(
        self,
        updates: list[tuple[object, object]],
        side: Literal["bid", "ask"],
    ) -> None:
        """Apply (price, qty) updates; qty <= 0 removes the level."""
        book = self._bids if side == SIDE_BID else self._asks
        for p, q in updates:
            price, qty = _d(p), _d(q)
            if qty <= 0:
                book.pop(price, None)
            else:
                book[price] = qty

    def sorted_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._bids.items(), key=lambda x: x[0], reverse=True)

    def sorted_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._asks.items(), key=lambda x: x[0])

    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        sb = self.sorted_bids()
        return sb[0] if sb else None

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        sa = self.sorted_asks()
        return sa[0] if sa else None

    def to_normalized_dict(self, symbol: str, timestamp_ms: int | None = None) -> dict:
        """Same core keys as :meth:`exchange.client.ExchangeClient.fetch_order_book`."""
        bids = self.sorted_bids()
        asks = self.sorted_asks()
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        mid_price: Decimal | None = None
        spread_bps: Decimal | None = None
        if best_bid is not None and best_ask is not None:
            bp, _ = best_bid
            ap, _ = best_ask
            mid_price = (bp + ap) / Decimal("2")
            if mid_price > 0:
                spread_bps = (ap - bp) / mid_price * Decimal("10000")
        return {
            "symbol": symbol,
            "timestamp": timestamp_ms,
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread_bps": spread_bps,
            "last_update_id": self._sequence_id,
            "nonce": self._sequence_id,
        }
