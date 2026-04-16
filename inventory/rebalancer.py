# inventory/rebalancer.py

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal

from inventory.tracker import InventoryTracker, Venue

# Hardcoded for testnet / estimation purposes
TRANSFER_FEES: dict[str, dict] = {
    "ETH": {
        "withdrawal_fee": Decimal("0.005"),
        "min_withdrawal": Decimal("0.01"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDT": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDC": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
}

# Keep at least this much at each venue to continue trading (same asset units)
MIN_OPERATING_BALANCE: dict[str, Decimal] = {
    "ETH": Decimal("0.5"),
    "USDT": Decimal("500"),
    "USDC": Decimal("500"),
}

# Reference USD (estimation only — configure for your environment)
REFERENCE_USD_PER_ETH = Decimal("2000")
REFERENCE_USD_PER_STABLE = Decimal("1")


@dataclass
class TransferPlan:
    """A planned transfer between venues."""

    from_venue: Venue
    to_venue: Venue
    asset: str
    amount: Decimal
    estimated_fee: Decimal  # Withdrawal/gas fee (same asset as `amount`)
    estimated_time_min: int  # Minutes to complete

    @property
    def net_amount(self) -> Decimal:
        """Amount received after fees."""
        return self.amount - self.estimated_fee


def _fee_usd(asset: str, fee_amt: Decimal) -> Decimal:
    if asset == "ETH":
        return fee_amt * REFERENCE_USD_PER_ETH
    if asset in ("USDT", "USDC"):
        return fee_amt * REFERENCE_USD_PER_STABLE
    return fee_amt * REFERENCE_USD_PER_STABLE


def _normalize_target_ratio(
    venues: list[Venue],
    target_ratio: dict[Venue, float] | None,
) -> dict[Venue, Decimal]:
    if not venues:
        return {}
    if target_ratio is None:
        w = Decimal("1") / Decimal(len(venues))
        return {v: w for v in venues}
    raw = {v: Decimal(str(target_ratio.get(v, 0.0))) for v in venues}
    s = sum(raw.values(), Decimal("0"))
    if s <= 0:
        w = Decimal("1") / Decimal(len(venues))
        return {v: w for v in venues}
    return {v: raw[v] / s for v in venues}


class RebalancePlanner:
    """
    Generates rebalancing plans when inventory skew exceeds threshold.
    Plans only — does NOT execute transfers.
    """

    def __init__(
        self,
        tracker: InventoryTracker,
        threshold_pct: float = 30.0,
        target_ratio: dict[Venue, float] | None = None,
    ):
        self._tracker = tracker
        self._threshold = float(threshold_pct)
        self._target_ratio = target_ratio

    def _targets(self, asset: str) -> tuple[dict[Venue, Decimal], Decimal, dict[Venue, Decimal]]:
        sk = self._tracker.skew(asset, self._threshold)
        venues = list(self._tracker.venues)
        ratios = _normalize_target_ratio(venues, self._target_ratio)
        total = sk["total"]
        amounts = {Venue(k): sk["venues"][k]["amount"] for k in sk["venues"]}
        targets = {v: total * ratios[v] for v in venues}
        return targets, total, amounts

    def check_all(self) -> list[dict]:
        """
        Check all tracked assets for skew.
        """
        rows = []
        for s in self._tracker.get_skews(self._threshold):
            rows.append(
                {
                    "asset": s["asset"],
                    "max_deviation_pct": s["max_deviation_pct"],
                    "needs_rebalance": s["needs_rebalance"],
                }
            )
        return rows

    def plan(self, asset: str) -> list[TransferPlan]:
        """
        Generate transfer plan to rebalance a specific asset.

        Fee model: source loses ``amount + estimated_fee``;
        destination gains ``amount - estimated_fee``.
        """
        sk = self._tracker.skew(asset, self._threshold)
        if not sk["needs_rebalance"] or sk["total"] <= 0:
            return []

        fee_info = TRANSFER_FEES.get(asset)
        if fee_info is None:
            return []

        withdrawal_fee: Decimal = fee_info["withdrawal_fee"]
        min_wd: Decimal = fee_info["min_withdrawal"]
        eta: int = int(fee_info["estimated_time_min"])

        targets, _total, amounts = self._targets(asset)
        venues = list(self._tracker.venues)
        if len(venues) < 2:
            return []

        diffs = {v: amounts[v] - targets[v] for v in venues}
        from_v = max(venues, key=lambda v: diffs[v])
        to_v = min(venues, key=lambda v: diffs[v])
        if diffs[from_v] <= 0 or diffs[to_v] >= 0:
            return []

        # Gross amount G: destination receives G - withdrawal_fee; want destination ~= target
        target_to = targets[to_v]
        cur_to = amounts[to_v]
        g_ideal = target_to - cur_to + withdrawal_fee

        if g_ideal <= 0:
            return []

        # Source must retain min operating balance after paying G + withdrawal_fee
        from_amt = amounts[from_v]
        g_max = from_amt - withdrawal_fee - MIN_OPERATING_BALANCE.get(asset, Decimal("0"))
        g = min(g_ideal, g_max)

        if g < min_wd:
            return []
        if g <= 0:
            return []

        return [
            TransferPlan(
                from_venue=from_v,
                to_venue=to_v,
                asset=asset,
                amount=g,
                estimated_fee=withdrawal_fee,
                estimated_time_min=eta,
            )
        ]

    def plan_all(self) -> dict[str, list[TransferPlan]]:
        out: dict[str, list[TransferPlan]] = {}
        for row in self.check_all():
            if not row["needs_rebalance"]:
                continue
            a = row["asset"]
            plans = self.plan(a)
            if plans:
                out[a] = plans
        return out

    def estimate_cost(self, plans: list[TransferPlan]) -> dict:
        """
        Estimate total cost of executing rebalance plans.
        """
        total_usd = Decimal("0")
        max_time = 0
        for p in plans:
            total_usd += _fee_usd(p.asset, p.estimated_fee)
            max_time = max(max_time, p.estimated_time_min)
        assets_affected = sorted({p.asset for p in plans})
        return {
            "total_transfers": len(plans),
            "total_fees_usd": total_usd,
            "total_time_min": max_time,
            "assets_affected": assets_affected,
        }


def _fmt_dec(x: Decimal, places: int = 4) -> str:
    q = Decimal("1").scaleb(-places)
    return str(x.quantize(q))


def _run_check(planner: RebalancePlanner) -> None:
    print()
    print("Inventory Skew Report")
    print("═" * 43)
    for s in planner._tracker.get_skews(planner._threshold):
        asset = s["asset"]
        print(f"Asset: {asset}")
        for vkey, vdata in s["venues"].items():
            pct = vdata["pct"]
            dev = vdata["deviation_pct"]
            amt = vdata["amount"]
            line = (
                f"  {vkey.capitalize():<8}  {_fmt_dec(amt)} {asset}  "
                f"({pct:.0f}%)   deviation: {dev:+.1f}%"
            )
            print(line)
        if s["needs_rebalance"]:
            print("  Status: NEEDS REBALANCE")
        else:
            print(f"  Status: OK (max deviation: {s['max_deviation_pct']:.1f}%)")
        print()
    print("═" * 43)
    print()


def _run_plan(planner: RebalancePlanner, asset: str) -> None:
    plans = planner.plan(asset)
    print()
    print(f"Rebalance Plan: {asset}")
    print("─" * 43)
    if not plans:
        print("  (no transfers planned)")
        print()
        return
    cost = planner.estimate_cost(plans)
    for i, p in enumerate(plans, start=1):
        fee_usd = _fee_usd(p.asset, p.estimated_fee)
        print(f"Transfer {i}:")
        print(f"  From:     {p.from_venue.value}")
        print(f"  To:       {p.to_venue.value}")
        print(f"  Amount:   {_fmt_dec(p.amount)} {p.asset}")
        print(f"  Fee:      {_fmt_dec(p.estimated_fee)} {p.asset} (~${_fmt_dec(fee_usd, 2)})")
        print(f"  ETA:      ~{p.estimated_time_min} min")
        print()
    print(f"Estimated total cost: ${_fmt_dec(cost['total_fees_usd'], 2)}")
    print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inventory rebalance check / plan")
    parser.add_argument("--check", action="store_true", help="Print skew report")
    parser.add_argument("--plan", metavar="ASSET", help="Print rebalance plan for ASSET (e.g. ETH)")
    args = parser.parse_args(argv)

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    planner = RebalancePlanner(tracker)

    if args.check:
        _run_check(planner)
    if args.plan:
        _run_plan(planner, args.plan.upper())
    if not args.check and not args.plan:
        parser.print_help()


if __name__ == "__main__":
    main()
