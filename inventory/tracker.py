# inventory/tracker.py

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

DEFAULT_REBALANCE_DEVIATION_THRESHOLD_PCT = 30.0


class Venue(str, Enum):
    BINANCE = "binance"
    WALLET = "wallet"  # On-chain wallet (DEX venue)


@dataclass
class Balance:
    venue: Venue
    asset: str
    free: Decimal
    locked: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


def _to_decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


class InventoryTracker:
    """
    Tracks positions across CEX and DEX venues.
    Single source of truth for where your money is.
    """

    def __init__(self, venues: list[Venue]):
        """Initialize tracker for given venues."""
        self._venues = list(venues)
        self._balances: dict[Venue, dict[str, Balance]] = {v: {} for v in self._venues}

    @property
    def venues(self) -> list[Venue]:
        return list(self._venues)

    def update_from_cex(self, venue: Venue, balances: dict):
        """
        Update balances from ExchangeClient.fetch_balance().
        Replaces previous snapshot for this venue.

        Args:
            venue: Which CEX venue
            balances: {asset: {free, locked, total}} from ExchangeClient
        """
        if venue not in self._balances:
            self._venues.append(venue)
            self._balances[venue] = {}
        new_map: dict[str, Balance] = {}
        for asset, row in balances.items():
            if not isinstance(row, dict):
                continue
            free = _to_decimal(row.get("free", 0))
            locked = _to_decimal(row.get("locked", row.get("used", 0)))
            total_raw = row.get("total")
            total = free + locked if total_raw is None else _to_decimal(total_raw)
            if total == 0 and free == 0 and locked == 0:
                continue
            new_map[asset] = Balance(venue=venue, asset=asset, free=free, locked=locked)
        self._balances[venue] = new_map

    def update_from_wallet(self, venue: Venue, balances: dict):
        """
        Update balances from on-chain wallet query.

        Args:
            venue: Wallet venue
            balances: {asset: amount} from chain/ module
        """
        if venue not in self._balances:
            self._venues.append(venue)
            self._balances[venue] = {}
        new_map: dict[str, Balance] = {}
        for asset, amount in balances.items():
            amt = _to_decimal(amount)
            if amt == 0:
                continue
            new_map[asset] = Balance(venue=venue, asset=asset, free=amt, locked=Decimal("0"))
        self._balances[venue] = new_map

    def snapshot(self, usd_prices: dict[str, Decimal] | None = None) -> dict:
        """
        Full portfolio snapshot at current time.

        If ``usd_prices`` is provided (asset -> USD), ``total_usd`` is included;
        otherwise it is omitted.
        """
        venues_out: dict[str, dict] = {}
        totals: dict[str, Decimal] = {}

        for v in self._venues:
            key = v.value
            venues_out[key] = {}
            for asset, bal in self._balances.get(v, {}).items():
                venues_out[key][asset] = {
                    "free": bal.free,
                    "locked": bal.locked,
                    "total": bal.total,
                }
                totals[asset] = totals.get(asset, Decimal("0")) + bal.total

        out: dict = {
            "timestamp": datetime.utcnow(),
            "venues": venues_out,
            "totals": totals,
        }
        if usd_prices is not None:
            total_usd = Decimal("0")
            for asset, qty in totals.items():
                px = usd_prices.get(asset)
                if px is not None:
                    total_usd += qty * px
            out["total_usd"] = total_usd
        return out

    def get_available(self, venue: Venue, asset: str) -> Decimal:
        """
        How much of `asset` is available to trade at `venue`.
        Returns free balance only (not locked in orders).
        """
        b = self._balances.get(venue, {}).get(asset)
        return b.free if b else Decimal("0")

    def can_execute(
        self,
        buy_venue: Venue,
        buy_asset: str,
        buy_amount: Decimal,
        sell_venue: Venue,
        sell_asset: str,
        sell_amount: Decimal,
    ) -> dict:
        """
        Pre-flight check: can we execute both legs of an arb?
        """
        buy_need = _to_decimal(buy_amount)
        sell_need = _to_decimal(sell_amount)
        buy_avail = self.get_available(buy_venue, buy_asset)
        sell_avail = self.get_available(sell_venue, sell_asset)
        ok_buy = buy_avail >= buy_need
        ok_sell = sell_avail >= sell_need
        can = ok_buy and ok_sell
        reason = None
        if not ok_buy and not ok_sell:
            reason = "insufficient buy and sell venue balances"
        elif not ok_buy:
            reason = f"insufficient {buy_asset} on {buy_venue.value}"
        elif not ok_sell:
            reason = f"insufficient {sell_asset} on {sell_venue.value}"
        return {
            "can_execute": can,
            "buy_venue_available": buy_avail,
            "buy_venue_needed": buy_need,
            "sell_venue_available": sell_avail,
            "sell_venue_needed": sell_need,
            "reason": reason,
        }

    def _ensure_balance(self, venue: Venue, asset: str) -> Balance:
        if venue not in self._balances:
            self._venues.append(venue)
            self._balances[venue] = {}
        if asset not in self._balances[venue]:
            self._balances[venue][asset] = Balance(
                venue=venue, asset=asset, free=Decimal("0"), locked=Decimal("0")
            )
        return self._balances[venue][asset]

    def record_trade(
        self,
        venue: Venue,
        side: str,
        base_asset: str,
        quote_asset: str,
        base_amount: Decimal,
        quote_amount: Decimal,
        fee: Decimal,
        fee_asset: str,
    ):
        """
        Update internal balances after a trade executes.
        Must handle: buy increases base / decreases quote,
                     sell decreases base / increases quote,
                     fee deducted from fee_asset.
        """
        side_l = side.lower()
        base_amt = _to_decimal(base_amount)
        quote_amt = _to_decimal(quote_amount)
        fee_d = _to_decimal(fee)

        if side_l == "buy":
            self._ensure_balance(venue, base_asset).free += base_amt
            self._ensure_balance(venue, quote_asset).free -= quote_amt
        elif side_l == "sell":
            self._ensure_balance(venue, base_asset).free -= base_amt
            self._ensure_balance(venue, quote_asset).free += quote_amt
        else:
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        fee_b = self._ensure_balance(venue, fee_asset)
        fee_b.free -= fee_d

    def _target_fraction(self, venue: Venue) -> float:
        n = len(self._venues)
        if n == 0:
            return 0.0
        return 1.0 / n

    def skew(self, asset: str, rebalance_threshold_pct: float | None = None) -> dict:
        """
        Calculate distribution skew for an asset across venues.
        ``pct`` is 0–100. ``deviation_pct`` is relative vs equal-split target for that venue count.
        """
        amounts: dict[Venue, Decimal] = {v: Decimal("0") for v in self._venues}
        for v in self._venues:
            b = self._balances.get(v, {}).get(asset)
            if b:
                amounts[v] = b.total
        total = sum(amounts.values(), Decimal("0"))
        threshold = (
            rebalance_threshold_pct
            if rebalance_threshold_pct is not None
            else DEFAULT_REBALANCE_DEVIATION_THRESHOLD_PCT
        )

        venues_out: dict[str, dict] = {}
        max_dev = 0.0
        target_frac = self._target_fraction(self._venues[0]) if self._venues else 0.5

        for v in self._venues:
            amt = amounts[v]
            if total > 0:
                pct = float(amt / total * 100)
                actual_frac = float(amt / total)
            else:
                pct = 0.0
                actual_frac = 0.0
            if target_frac > 0:
                deviation_pct = abs(actual_frac - target_frac) / target_frac * 100.0
            else:
                deviation_pct = 0.0
            max_dev = max(max_dev, deviation_pct)
            venues_out[v.value] = {
                "amount": amt,
                "pct": pct,
                "deviation_pct": deviation_pct,
            }

        return {
            "asset": asset,
            "total": total,
            "venues": venues_out,
            "max_deviation_pct": max_dev,
            "needs_rebalance": max_dev > threshold,
        }

    def get_skews(self, rebalance_threshold_pct: float | None = None) -> list[dict]:
        """
        Check skew for ALL tracked assets.
        """
        assets: set[str] = set()
        for v in self._venues:
            assets |= set(self._balances.get(v, {}).keys())
        return [self.skew(a, rebalance_threshold_pct) for a in sorted(assets)]
