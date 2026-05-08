"""USD marks for inventory totals (shared by PnL fee USD and risk capital).

- :func:`reference_usd_per_unit` / :func:`estimate_inventory_usd` — pure,
  deterministic constants used by PnL fee math and the rebalancer (no CEX).
- :func:`live_usd_per_unit` / :func:`estimate_inventory_usd_live` — live CEX
  mid via ``ASSET/USDC`` then ``ASSET/USDT``. If no valid mid is obtained for a
  non-stable asset, :exc:`LiveUsdMarkError` is raised (no silent fallback to
  hardcoded token prices).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterable, Optional

from inventory.tracker import InventoryTracker, Venue

logger = logging.getLogger(__name__)

REFERENCE_USD_PER_ETH = Decimal("2000")
REFERENCE_USD_PER_STABLE = Decimal("1")
REFERENCE_USD_PER_BTC = Decimal("42000")

_STABLES = ("USDT", "USDC", "BUSD", "DAI", "USD₮0", "USDT0")
_LIVE_QUOTE_ORDER = ("USDC", "USDT")


class LiveUsdMarkError(RuntimeError):
    """Could not resolve a live USD price for an asset from the CEX order books."""


def reference_usd_per_unit(asset: str) -> Decimal:
    """Rough USD per one unit of *asset* for portfolio estimation only."""
    a = asset.upper()
    if a in _STABLES:
        return REFERENCE_USD_PER_STABLE
    if a in ("ETH", "WETH"):
        return REFERENCE_USD_PER_ETH
    if a == "BTC":
        return REFERENCE_USD_PER_BTC
    return REFERENCE_USD_PER_STABLE


def estimate_inventory_usd(tracker: InventoryTracker) -> Decimal:
    """Sum balances across venues using :func:`reference_usd_per_unit`."""
    snap = tracker.snapshot()
    totals = snap.get("totals") or {}
    total = Decimal("0")
    for asset, qty in totals.items():
        total += qty * reference_usd_per_unit(asset)
    return total


def _book_mid(exchange: Any, symbol: str) -> Optional[Decimal]:
    """Return mid price from ``exchange.fetch_order_book`` or ``None``."""
    try:
        ob = exchange.fetch_order_book(symbol)
    except Exception as exc:
        logger.debug("usd_mark: fetch_order_book(%s) failed: %s", symbol, exc)
        return None
    mid = ob.get("mid_price") if isinstance(ob, dict) else None
    if mid is None:
        return None
    try:
        m = Decimal(str(mid))
    except Exception:
        return None
    return m if m > 0 else None


def live_usd_per_unit(
    asset: str,
    exchange: Any,
    *,
    quote_order: Iterable[str] = _LIVE_QUOTE_ORDER,
) -> Decimal:
    """Live USD price per unit of *asset* via CEX order books.

    Stables return ``$1``. For other assets the function tries
    ``ASSET/<quote>`` for each quote in *quote_order* (default ``USDC`` then
    ``USDT``) and returns the first positive order-book mid.

    Raises:
        LiveUsdMarkError: If *exchange* is missing or no pair yields a valid mid.
    """
    a = asset.upper()
    if a in _STABLES:
        return REFERENCE_USD_PER_STABLE
    if exchange is None:
        raise LiveUsdMarkError("live_usd_per_unit: exchange is None (non-stable asset)")
    base_for_quote = "ETH" if a == "WETH" else a
    tried: list[str] = []
    for quote in quote_order:
        if quote == base_for_quote:
            continue
        sym = f"{base_for_quote}/{quote}"
        tried.append(sym)
        mid = _book_mid(exchange, sym)
        if mid is not None:
            return mid
    raise LiveUsdMarkError(
        f"could not resolve live USD mark for {asset!r}; tried CEX symbols: {tried}",
    )


def estimate_inventory_usd_live(
    tracker: InventoryTracker,
    exchange: Any,
) -> Decimal:
    """Sum balances across venues using :func:`live_usd_per_unit`.

    Skips assets with zero total quantity. The price for each distinct non-stable
    asset is fetched once per call (one CEX REST hit per asset).

    Raises:
        LiveUsdMarkError: propagated from :func:`live_usd_per_unit` when pricing fails.
    """
    snap = tracker.snapshot()
    totals = snap.get("totals") or {}
    cache: dict[str, Decimal] = {}
    total = Decimal("0")
    for asset, qty in totals.items():
        q = Decimal(str(qty))
        if q == 0:
            continue
        a = asset.upper()
        if a not in cache:
            cache[a] = live_usd_per_unit(a, exchange)
        total += q * cache[a]
    return total


def _symbols_for_pair_leg(logical: str) -> tuple[str, ...]:
    """Balance keys that roll up to one pair leg (e.g. WETH counts as ETH)."""
    u = logical.upper()
    if u == "ETH":
        return ("ETH", "WETH", "WBETH")
    if u == "BTC":
        return ("BTC", "WBTC")
    if u == "USDT":
        return ("USDT", "USD₮0", "USDT0")
    return (u,)


def _venue_leg_qty(venues: dict[str, Any], venue_key: str, leg_symbols: tuple[str, ...]) -> Decimal:
    assets = venues.get(venue_key) or {}
    want = {s.upper() for s in leg_symbols}
    total = Decimal("0")
    for sym, row in assets.items():
        if str(sym).upper() not in want:
            continue
        if isinstance(row, dict):
            total += Decimal(str(row.get("total", "0")))
    return total


def snapshot_pair_mtm_usd(
    tracker: InventoryTracker,
    exchange: Any,
    *,
    cex_venue: Venue,
    pair: str,
) -> dict[str, Any]:
    """Per-venue MTM for the two pair legs using the same CEX marks as :func:`live_usd_per_unit`.

    Uses ``InventoryTracker.snapshot`` totals (free+locked) per venue. Raises
    :exc:`LiveUsdMarkError` if a non-stable leg cannot be priced from the CEX.
    """
    if "/" not in pair:
        raise ValueError(f"pair must be BASE/QUOTE, got {pair!r}")
    parts = pair.split("/")
    base_l, quote_l = parts[0].strip().upper(), parts[1].strip().upper()
    base_syms = _symbols_for_pair_leg(base_l)
    quote_syms = _symbols_for_pair_leg(quote_l)

    snap = tracker.snapshot()
    venues = snap.get("venues") or {}
    wx, cx = Venue.WALLET.value, cex_venue.value

    dex_bq = _venue_leg_qty(venues, wx, base_syms)
    dex_qq = _venue_leg_qty(venues, wx, quote_syms)
    cex_bq = _venue_leg_qty(venues, cx, base_syms)
    cex_qq = _venue_leg_qty(venues, cx, quote_syms)

    px_b = live_usd_per_unit(base_l, exchange)
    px_q = live_usd_per_unit(quote_l, exchange)

    dex_b_usd = dex_bq * px_b
    dex_q_usd = dex_qq * px_q
    cex_b_usd = cex_bq * px_b
    cex_q_usd = cex_qq * px_q

    return {
        "pair": pair,
        "base": base_l,
        "quote": quote_l,
        "dex_base_qty": dex_bq,
        "dex_quote_qty": dex_qq,
        "cex_base_qty": cex_bq,
        "cex_quote_qty": cex_qq,
        "dex_base_usd": dex_b_usd,
        "dex_quote_usd": dex_q_usd,
        "cex_base_usd": cex_b_usd,
        "cex_quote_usd": cex_q_usd,
        "dex_total_usd": dex_b_usd + dex_q_usd,
        "cex_total_usd": cex_b_usd + cex_q_usd,
    }
