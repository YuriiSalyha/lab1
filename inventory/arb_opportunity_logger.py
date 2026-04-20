"""
Log arbitrage opportunities (DEX–CEX or CEX–CEX) with CSV export.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

ArbKind = Literal["dex_cex", "cex_cex"]

# Minimum spread (bps) to record for CEX–CEX helper (avoid log spam).
MIN_CEX_CEX_SPREAD_BPS_DEFAULT = Decimal("1")


def _json_extra(obj: Any) -> str:
    if obj is None:
        return ""
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


@dataclass
class ArbOpportunityRecord:
    timestamp: datetime
    kind: ArbKind
    pair: str
    direction: str
    gap_bps: Decimal
    estimated_net_pnl_bps: Decimal
    executable: bool
    cex_venue: str = ""
    extra_json: str = ""

    def as_csv_row(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind,
            "pair": self.pair,
            "direction": self.direction,
            "gap_bps": str(self.gap_bps),
            "estimated_net_pnl_bps": str(self.estimated_net_pnl_bps),
            "executable": str(self.executable).lower(),
            "cex_venue": self.cex_venue,
            "extra_json": self.extra_json,
        }


@dataclass
class ArbOpportunityLogger:
    """In-memory log + CSV export."""

    records: list[ArbOpportunityRecord] = field(default_factory=list)

    def append(self, rec: ArbOpportunityRecord) -> None:
        self.records.append(rec)

    def append_from_arb_check(
        self,
        result: dict[str, Any],
        *,
        cex_venue: str = "binance",
    ) -> ArbOpportunityRecord:
        """
        Map output of ``ArbChecker.check()`` to a record.
        """
        ts = result["timestamp"]
        if not isinstance(ts, datetime):
            ts = datetime.utcnow()
        details = result.get("details") or {}
        extra = {
            "dex_price": str(result.get("dex_price", "")),
            "cex_bid": str(result.get("cex_bid", "")),
            "cex_ask": str(result.get("cex_ask", "")),
            "estimated_costs_bps": str(result.get("estimated_costs_bps", "")),
            "inventory_ok": result.get("inventory_ok"),
            "details": details,
        }
        venue_s = result.get("cex_venue")
        rec = ArbOpportunityRecord(
            timestamp=ts,
            kind="dex_cex",
            pair=str(result.get("pair", "")),
            direction=str(result.get("direction", "")),
            gap_bps=Decimal(str(result.get("gap_bps", "0"))),
            estimated_net_pnl_bps=Decimal(str(result.get("estimated_net_pnl_bps", "0"))),
            executable=bool(result.get("executable")),
            cex_venue=str(venue_s) if venue_s is not None else cex_venue,
            extra_json=_json_extra(extra),
        )
        self.append(rec)
        return rec

    def append_cex_cex_spread(
        self,
        *,
        symbol_a: str,
        symbol_b: str,
        mid_a: Decimal,
        mid_b: Decimal,
        venue_a: str,
        venue_b: str,
        min_spread_bps: Decimal = MIN_CEX_CEX_SPREAD_BPS_DEFAULT,
    ) -> ArbOpportunityRecord | None:
        """
        If absolute mid spread vs mean mid exceeds ``min_spread_bps``, append a ``cex_cex`` row.
        """
        if mid_a <= 0 or mid_b <= 0:
            return None
        m = (mid_a + mid_b) / Decimal("2")
        spread_bps = abs(mid_a - mid_b) / m * Decimal("10000")
        if spread_bps < min_spread_bps:
            return None
        pair = f"{symbol_a}|{symbol_b}"
        rec = ArbOpportunityRecord(
            timestamp=datetime.utcnow(),
            kind="cex_cex",
            pair=pair,
            direction=f"high@{venue_a}" if mid_a > mid_b else f"high@{venue_b}",
            gap_bps=spread_bps,
            estimated_net_pnl_bps=spread_bps,
            executable=False,
            cex_venue=f"{venue_a}+{venue_b}",
            extra_json=_json_extra(
                {"mid_a": str(mid_a), "mid_b": str(mid_b), "venue_a": venue_a, "venue_b": venue_b},
            ),
        )
        self.append(rec)
        return rec

    def export_csv(self, filepath: str | Path) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "timestamp",
            "kind",
            "pair",
            "direction",
            "gap_bps",
            "estimated_net_pnl_bps",
            "executable",
            "cex_venue",
            "extra_json",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in self.records:
                w.writerow(r.as_csv_row())


def records_to_table_rows(snapshot: dict[str, Any]) -> list[list[str]]:
    """
    Build display rows from :meth:`inventory.tracker.InventoryTracker.snapshot` (for tests / CLI).
    """
    rows: list[list[str]] = []
    venues = snapshot.get("venues") or {}
    for vname, assets in sorted(venues.items()):
        for asset, amap in sorted(assets.items()):
            if not isinstance(amap, dict):
                continue
            rows.append(
                [
                    vname,
                    asset,
                    str(amap.get("free", "")),
                    str(amap.get("locked", "")),
                    str(amap.get("total", "")),
                ],
            )
    return rows
