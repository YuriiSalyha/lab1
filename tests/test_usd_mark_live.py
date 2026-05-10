"""Live USD mark helpers in :mod:`inventory.usd_mark`."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from inventory.tracker import InventoryTracker, Venue
from inventory.usd_mark import (
    REFERENCE_USD_PER_STABLE,
    LiveUsdMarkError,
    estimate_inventory_usd_live,
    live_usd_per_unit,
    snapshot_pair_mtm_usd,
)


class _FakeExchange:
    """Minimal ``fetch_order_book`` substitute keyed by symbol."""

    def __init__(self, books: dict[str, dict[str, Any] | Exception]) -> None:
        self._books = books
        self.calls: list[str] = []

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        self.calls.append(symbol)
        v = self._books.get(symbol)
        if v is None:
            raise RuntimeError(f"no symbol {symbol}")
        if isinstance(v, Exception):
            raise v
        return v


def _book(mid: str) -> dict[str, Any]:
    return {"mid_price": Decimal(mid)}


def test_live_usd_per_unit_uses_usdc_first() -> None:
    """ETH/USDC is queried before ETH/USDT; ETH/USDT is never called when USDC succeeds."""
    ex = _FakeExchange({"ETH/USDC": _book("2345.67"), "ETH/USDT": _book("9999")})
    px = live_usd_per_unit("ETH", ex)
    assert px == Decimal("2345.67")
    assert ex.calls == ["ETH/USDC"]


def test_live_usd_per_unit_falls_back_to_usdt_when_usdc_missing() -> None:
    ex = _FakeExchange({"ETH/USDT": _book("2350.00")})
    px = live_usd_per_unit("ETH", ex)
    assert px == Decimal("2350.00")
    assert ex.calls == ["ETH/USDC", "ETH/USDT"]


def test_live_usd_per_unit_raises_when_no_valid_mid() -> None:
    ex = _FakeExchange({})
    with pytest.raises(LiveUsdMarkError, match="could not resolve live USD mark"):
        live_usd_per_unit("ETH", ex)


def test_live_usd_per_unit_raises_when_exchange_none() -> None:
    with pytest.raises(LiveUsdMarkError, match="exchange is None"):
        live_usd_per_unit("ETH", None)


def test_live_usd_per_unit_stables_skip_exchange() -> None:
    ex = _FakeExchange({})
    assert live_usd_per_unit("USDT", ex) == REFERENCE_USD_PER_STABLE
    assert live_usd_per_unit("USDC", ex) == REFERENCE_USD_PER_STABLE
    assert ex.calls == []


def test_live_usd_per_unit_weth_normalized_to_eth_pair() -> None:
    """``WETH`` is priced via ``ETH/USDC`` since CEXes don't list ``WETH/USDC``."""
    ex = _FakeExchange({"ETH/USDC": _book("2400")})
    assert live_usd_per_unit("WETH", ex) == Decimal("2400")
    assert ex.calls == ["ETH/USDC"]


def _tracker_eth_usdt(eth: str, usdt: str) -> InventoryTracker:
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": Decimal(eth), "locked": Decimal("0")},
            "USDT": {"free": Decimal(usdt), "locked": Decimal("0")},
        },
    )
    t.update_from_wallet(Venue.WALLET, {"ETH": Decimal(eth), "USDT": Decimal(usdt)})
    return t


def test_estimate_inventory_usd_live_aggregates_dex_and_cex() -> None:
    """Sum DEX (wallet) + CEX balances using one live ETH price."""
    ex = _FakeExchange({"ETH/USDC": _book("2000")})
    tr = _tracker_eth_usdt("0.025", "25")
    # 0.05 ETH * 2000 + 50 USDT * 1 = 150
    assert estimate_inventory_usd_live(tr, ex) == Decimal("150")
    # Only one ETH/USDC fetch, no USDT call.
    assert ex.calls == ["ETH/USDC"]


def test_estimate_inventory_usd_live_raises_when_pricing_fails() -> None:
    ex = _FakeExchange({"ETH/USDC": RuntimeError("boom"), "ETH/USDT": RuntimeError("nope")})
    tr = _tracker_eth_usdt("0.05", "25")
    with pytest.raises(LiveUsdMarkError, match="could not resolve live USD mark"):
        estimate_inventory_usd_live(tr, ex)


def test_snapshot_pair_mtm_usd_wallet_vs_cex() -> None:
    ex = _FakeExchange({"ETH/USDC": _book("2000")})
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_wallet(Venue.WALLET, {"ETH": Decimal("0.01"), "USDT": Decimal("10")})
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": Decimal("0.02"), "locked": Decimal("0")},
            "USDT": {"free": Decimal("20"), "locked": Decimal("0")},
        },
    )
    d = snapshot_pair_mtm_usd(t, ex, cex_venue=Venue.BINANCE, pair="ETH/USDT")
    assert d["dex_base_qty"] == Decimal("0.01")
    assert d["dex_quote_qty"] == Decimal("10")
    assert d["cex_base_qty"] == Decimal("0.02")
    assert d["cex_quote_qty"] == Decimal("20")
    assert d["dex_base_usd"] == Decimal("20")
    assert d["dex_quote_usd"] == Decimal("10") * REFERENCE_USD_PER_STABLE
    assert d["dex_total_usd"] == d["dex_base_usd"] + d["dex_quote_usd"]
    assert d["cex_total_usd"] == d["cex_base_usd"] + d["cex_quote_usd"]
