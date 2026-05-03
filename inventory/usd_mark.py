"""Reference USD marks for inventory totals (shared by PnL fee USD and risk capital)."""

from __future__ import annotations

from decimal import Decimal

from inventory.tracker import InventoryTracker

REFERENCE_USD_PER_ETH = Decimal("2000")
REFERENCE_USD_PER_STABLE = Decimal("1")
REFERENCE_USD_PER_BTC = Decimal("42000")


def reference_usd_per_unit(asset: str) -> Decimal:
    """Rough USD per one unit of *asset* for portfolio estimation only."""
    a = asset.upper()
    if a in ("USDT", "USDC", "BUSD", "DAI"):
        return REFERENCE_USD_PER_STABLE
    if a in ("ETH", "WETH"):
        return REFERENCE_USD_PER_ETH
    if a == "BTC":
        return REFERENCE_USD_PER_BTC
    return REFERENCE_USD_PER_STABLE


def estimate_inventory_usd(tracker: InventoryTracker) -> Decimal:
    """Sum balances across venues using :func:`reference_usd_per_unit` (aggregated by asset)."""
    snap = tracker.snapshot()
    totals = snap.get("totals") or {}
    total = Decimal("0")
    for asset, qty in totals.items():
        total += qty * reference_usd_per_unit(asset)
    return total
