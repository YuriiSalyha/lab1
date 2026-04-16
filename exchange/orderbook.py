"""
Order book analysis and CLI (``python -m exchange.orderbook SYMBOL [--depth N]``).
"""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timezone
from decimal import Decimal


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _fmt_usd(d: Decimal) -> str:
    return f"${d:,.2f}"


def _fmt_qty(d: Decimal, max_dp: int = 4) -> str:
    s = f"{d:,.{max_dp}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _imbalance_label(ratio: float) -> str:
    if ratio > 0.25:
        return "buy pressure"
    if ratio < -0.25:
        return "sell pressure"
    if abs(ratio) < 0.05:
        return "balanced"
    return "slight buy pressure" if ratio > 0 else "slight sell pressure"


class OrderBookAnalyzer:
    """
    Analyze order book snapshots for trading decisions.

    Expects the normalized dict from :meth:`exchange.client.ExchangeClient.fetch_order_book`.
    """

    def __init__(self, orderbook: dict):
        """
        Initialize with order book from ExchangeClient.fetch_order_book().
        """
        self.orderbook = orderbook

    def walk_the_book(
        self,
        side: str,  # "buy" (walk asks) or "sell" (walk bids)
        qty: float,
    ) -> dict:
        """
        Simulate filling ``qty`` base against the book.

        Returns:
            avg_price, total_cost (quote), slippage_bps vs best touch,
            levels_consumed (count of book levels **fully exhausted**,
            i.e. entire quantity at that price taken; the final partial
            level does not increment this),
            fully_filled, fills.
        """
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        qty_d = _d(qty)
        if qty_d <= 0:
            raise ValueError("qty must be positive")

        if side == "buy":
            book = list(self.orderbook.get("asks") or [])
        else:
            book = list(self.orderbook.get("bids") or [])

        if not book:
            return {
                "avg_price": Decimal("0"),
                "total_cost": Decimal("0"),
                "slippage_bps": Decimal("0"),
                "levels_consumed": 0,
                "fully_filled": False,
                "fills": [],
            }

        best_price = _d(book[0][0])
        remaining = qty_d
        fills: list[dict] = []
        levels_consumed = 0

        for price, quantity in book:
            if remaining <= 0:
                break
            p = _d(price)
            q = _d(quantity)
            if q <= 0:
                continue
            take = min(q, remaining)
            cost = p * take
            fills.append({"price": p, "qty": take, "cost": cost})
            if take == q:
                levels_consumed += 1
            remaining -= take

        if not fills:
            return {
                "avg_price": Decimal("0"),
                "total_cost": Decimal("0"),
                "slippage_bps": Decimal("0"),
                "levels_consumed": 0,
                "fully_filled": False,
                "fills": [],
            }

        total_qty = sum(f["qty"] for f in fills)
        total_cost = sum(f["cost"] for f in fills)
        avg_price = total_cost / total_qty

        if side == "buy":
            slippage_bps = (avg_price - best_price) / best_price * Decimal("10000")
        else:
            slippage_bps = (best_price - avg_price) / best_price * Decimal("10000")

        fully_filled = remaining <= Decimal("0")

        return {
            "avg_price": avg_price,
            "total_cost": total_cost,
            "slippage_bps": slippage_bps,
            "levels_consumed": levels_consumed,
            "fully_filled": fully_filled,
            "fills": fills,
        }

    def _depth_base_and_quote(self, side: str, bps: float) -> tuple[Decimal, Decimal]:
        """Sum base quantity and quote notional within ``bps`` of the best price."""
        bps_d = _d(bps)
        if bps_d < 0:
            raise ValueError("bps must be non-negative")

        if side == "bid":
            levels = self.orderbook.get("bids") or []
            if not levels:
                return Decimal("0"), Decimal("0")
            best = _d(levels[0][0])
            floor_p = best * (Decimal("1") - bps_d / Decimal("10000"))
            base = Decimal("0")
            quote = Decimal("0")
            for price, quantity in levels:
                p = _d(price)
                q = _d(quantity)
                if p >= floor_p:
                    base += q
                    quote += p * q
            return base, quote

        if side == "ask":
            levels = self.orderbook.get("asks") or []
            if not levels:
                return Decimal("0"), Decimal("0")
            best = _d(levels[0][0])
            ceil_p = best * (Decimal("1") + bps_d / Decimal("10000"))
            base = Decimal("0")
            quote = Decimal("0")
            for price, quantity in levels:
                p = _d(price)
                q = _d(quantity)
                if p <= ceil_p:
                    base += q
                    quote += p * q
            return base, quote

        raise ValueError("side must be 'bid' or 'ask'")

    def depth_at_bps(self, side: str, bps: float) -> Decimal:
        """
        Total base quantity available within ``bps`` basis points of the best price
        on that side (bids: downward from best bid; asks: upward from best ask).
        """
        base, _ = self._depth_base_and_quote(side, bps)
        return base

    def imbalance(self, levels: int = 10) -> float:
        """
        Imbalance in [-1.0, +1.0]: (bid_vol - ask_vol) / (bid_vol + ask_vol)
        over the first ``levels`` on each side.
        """
        if levels < 1:
            raise ValueError("levels must be >= 1")
        bids = (self.orderbook.get("bids") or [])[:levels]
        asks = (self.orderbook.get("asks") or [])[:levels]
        bid_vol = sum(_d(q[1]) for q in bids)
        ask_vol = sum(_d(q[1]) for q in asks)
        s = bid_vol + ask_vol
        if s == 0:
            return 0.0
        return float((bid_vol - ask_vol) / s)

    def quoted_spread_bps(self) -> Decimal:
        """(ask - bid) / mid * 10_000 using best touch; matches client if mid present."""
        if self.orderbook.get("spread_bps") is not None:
            return _d(self.orderbook["spread_bps"])
        bb = self.orderbook.get("best_bid")
        ba = self.orderbook.get("best_ask")
        if bb and ba:
            bp, ap = _d(bb[0]), _d(ba[0])
            m = (bp + ap) / Decimal("2")
            if m > 0:
                return (ap - bp) / m * Decimal("10000")
        return Decimal("0")

    def effective_spread(self, qty: float) -> Decimal:
        """
        Round-trip cost of immediacy for ``qty`` base (bps vs mid):
        (avg buy on asks - avg sell on bids) / mid * 10_000.
        """
        mid = self.orderbook.get("mid_price")
        if mid is None:
            bb = self.orderbook.get("best_bid")
            ba = self.orderbook.get("best_ask")
            if not bb or not ba:
                raise ValueError("order book missing mid and best bid/ask")
            mid = (_d(bb[0]) + _d(ba[0])) / Decimal("2")
        else:
            mid = _d(mid)
        if mid <= 0:
            raise ValueError("invalid mid price")

        buy_w = self.walk_the_book("buy", qty)
        sell_w = self.walk_the_book("sell", qty)
        if buy_w["avg_price"] <= 0 or sell_w["avg_price"] <= 0:
            return Decimal("0")
        return (buy_w["avg_price"] - sell_w["avg_price"]) / mid * Decimal("10000")


def _box_line(inner_width: int, text: str, pad: str = " ") -> str:
    inner = text[:inner_width].ljust(inner_width, pad)
    return f"║{inner}║"


def _print_report(
    symbol: str,
    ob: dict,
    analyzer: OrderBookAnalyzer,
    depth_bps: float,
    walk_sizes: list[float],
) -> None:
    base_ccy = symbol.split("/")[0] if "/" in symbol else "BASE"
    ts = ob.get("timestamp")
    if ts is not None:
        dt = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        ts_str = "—"

    bb = ob.get("best_bid")
    ba = ob.get("best_ask")
    mid = ob.get("mid_price")
    spread_abs = None
    spread_bps = ob.get("spread_bps")
    if bb and ba:
        bp, bq = _d(bb[0]), _d(bb[1])
        ap, aq = _d(ba[0]), _d(ba[1])
        spread_abs = ap - bp
    else:
        bp = ap = bq = aq = Decimal("0")

    if mid is None and bb and ba:
        mid = (_d(bb[0]) + _d(ba[0])) / Decimal("2")

    w = 54
    lines = [
        "╔" + "═" * w + "╗",
        _box_line(w, f"  {symbol} Order Book Analysis".ljust(w)),
        _box_line(w, f"  Timestamp: {ts_str}".ljust(w)),
        "╠" + "═" * w + "╣",
        _box_line(
            w,
            f"  Best Bid:    {_fmt_usd(bp)} × {_fmt_qty(bq)} {base_ccy}".ljust(w),
        ),
        _box_line(
            w,
            f"  Best Ask:    {_fmt_usd(ap)} × {_fmt_qty(aq)} {base_ccy}".ljust(w),
        ),
        _box_line(
            w,
            f"  Mid Price:   {_fmt_usd(mid) if mid is not None else '—'}".ljust(w),
        ),
    ]
    if spread_abs is not None and spread_bps is not None and mid:
        lines.append(
            _box_line(
                w,
                f"  Spread:      {_fmt_usd(spread_abs)} ({_fmt_qty(spread_bps, 2)} bps)".ljust(w),
            )
        )
    else:
        lines.append(_box_line(w, "  Spread:      —".ljust(w)))

    lines.append("╠" + "═" * w + "╣")
    lines.append(_box_line(w, f"  Depth (within {int(depth_bps)} bps):".ljust(w)))

    bid_base, bid_quote = analyzer._depth_base_and_quote("bid", depth_bps)
    ask_base, ask_quote = analyzer._depth_base_and_quote("ask", depth_bps)
    lines.append(
        _box_line(
            w,
            f"    Bids: {_fmt_qty(bid_base)} {base_ccy} ({_fmt_usd(bid_quote)})".ljust(w),
        )
    )
    lines.append(
        _box_line(
            w,
            f"    Asks: {_fmt_qty(ask_base)} {base_ccy} ({_fmt_usd(ask_quote)})".ljust(w),
        )
    )

    imb = analyzer.imbalance()
    lines.append(
        _box_line(
            w,
            f"  Imbalance: {imb:+.2f} ({_imbalance_label(imb)})".ljust(w),
        )
    )
    lines.append("╠" + "═" * w + "╣")

    for sz in walk_sizes:
        wtb = analyzer.walk_the_book("buy", sz)
        label = f"Walk-the-book ({_fmt_qty(_d(sz))} {base_ccy} buy):"
        lines.append(_box_line(w, f"  {label}".ljust(w)))
        lines.append(
            _box_line(
                w,
                f"    Avg price:  {_fmt_usd(wtb['avg_price'])}".ljust(w),
            )
        )
        lines.append(
            _box_line(
                w,
                f"    Slippage:   {_fmt_qty(wtb['slippage_bps'], 2)} bps".ljust(w),
            )
        )
        lines.append(
            _box_line(
                w,
                f"    Levels:     {wtb['levels_consumed']}".ljust(w),
            )
        )

    lines.append("╠" + "═" * w + "╣")
    es = analyzer.effective_spread(walk_sizes[0]) if walk_sizes else Decimal("0")
    walk_size_fmt = _fmt_qty(_d(walk_sizes[0]))
    es_bps_fmt = _fmt_qty(es, 1)
    lines.append(
        _box_line(
            w,
            f"  Effective spread ({walk_size_fmt} {base_ccy} round-trip): {es_bps_fmt} bps".ljust(
                w
            ),
        )
    )
    lines.append("╚" + "═" * w + "╝")

    print("\n".join(lines))


def _ensure_utf8_stdout() -> None:
    """Avoid UnicodeEncodeError on Windows consoles when printing box-drawing chars."""
    out = sys.stdout
    if hasattr(out, "reconfigure"):
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a Binance-style order book snapshot.")
    parser.add_argument("symbol", help='Market symbol, e.g. "ETH/USDT"')
    parser.add_argument("--depth", type=int, default=20, help="Depth levels to fetch (default 20)")
    parser.add_argument(
        "--depth-bps",
        type=float,
        default=10.0,
        help="Band for depth / imbalance context (default 10 bps)",
    )
    parser.add_argument(
        "--walk",
        type=str,
        default="2,10",
        help="Comma-separated base sizes for walk-the-book (default 2,10)",
    )
    args = parser.parse_args(argv)

    try:
        from config.config import BINANCE_CONFIG
        from exchange.client import ExchangeClient
    except ImportError as e:
        print(f"Import error: {e}", file=sys.stderr)
        return 1

    cfg = copy.deepcopy(BINANCE_CONFIG)
    try:
        client = ExchangeClient(cfg)
        ob = client.fetch_order_book(args.symbol, limit=args.depth)
    except Exception as e:
        print(f"Failed to fetch order book: {e}", file=sys.stderr)
        return 1

    analyzer = OrderBookAnalyzer(ob)
    try:
        walk_sizes = [float(x.strip()) for x in args.walk.split(",") if x.strip()]
    except ValueError:
        walk_sizes = [2.0, 10.0]
    if not walk_sizes:
        walk_sizes = [2.0, 10.0]

    _ensure_utf8_stdout()
    _print_report(args.symbol, ob, analyzer, args.depth_bps, walk_sizes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
