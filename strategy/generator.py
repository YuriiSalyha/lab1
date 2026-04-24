"""Signal generator: detect opportunity, validate fees, and check inventory.

Replaces and extends the detection-only :class:`scripts.arb_checker.ArbChecker`
with scoring, cooldown, inventory validation, and TTL-bound signals.

All calculations are performed in :class:`~decimal.Decimal`. The
:class:`ExchangeClient` already returns :class:`Decimal` prices; numeric config
values that a user might provide as ``float`` are coerced on entry.
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Optional

from inventory.tracker import InventoryTracker, Venue
from strategy.fees import BPS_DENOM, FeeStructure
from strategy.signal import Direction, Signal, to_decimal

logger = logging.getLogger(__name__)

DEFAULT_MIN_SPREAD_BPS = Decimal("50")
DEFAULT_MIN_PROFIT_USD = Decimal("5")
DEFAULT_MAX_POSITION_USD = Decimal("10000")
DEFAULT_SIGNAL_TTL_S = 5.0
DEFAULT_COOLDOWN_S = 2.0
# Probe base size when ``size`` is omitted (DEX quotes may depend on size).
DEFAULT_OPTIMAL_SIZE_PROBE_BASE = Decimal("0.01")
# Grid resolution when :class:`PricingEngine` quotes vary with trade size.
OPTIMAL_SIZE_GRID_SAMPLES = 12
CONFIG_MAX_TRADE_BASE = "max_trade_base"
# Multiplicative safety margin when checking that quote balance covers the buy.
INVENTORY_SAFETY_MULT = Decimal("1.01")
# Fallback DEX prices when no PricingEngine is wired; relative to CEX mid.
STUB_DEX_BUY_PREMIUM = Decimal("1.005")
STUB_DEX_SELL_PREMIUM = Decimal("1.008")

# Callable: pair string -> (base_token_obj, quote_token_obj) for PricingEngine quotes.
TokenResolver = Callable[[str], tuple[Any, Any]]


class SignalGenerator:
    """Produce validated :class:`Signal` instances from live market data."""

    def __init__(
        self,
        exchange_client: Any,
        pricing_module: Any,
        inventory_tracker: InventoryTracker,
        fee_structure: FeeStructure,
        config: Optional[dict] = None,
        *,
        token_resolver: Optional[TokenResolver] = None,
        cex_venue: Venue = Venue.BINANCE,
        wallet_venue: Venue = Venue.WALLET,
    ) -> None:
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.fees = fee_structure
        self.token_resolver = token_resolver
        self.cex_venue = cex_venue
        self.wallet_venue = wallet_venue

        cfg = config or {}
        self.min_spread_bps = to_decimal(cfg.get("min_spread_bps", DEFAULT_MIN_SPREAD_BPS))
        self.min_profit_usd = to_decimal(cfg.get("min_profit_usd", DEFAULT_MIN_PROFIT_USD))
        self.max_position_usd = to_decimal(cfg.get("max_position_usd", DEFAULT_MAX_POSITION_USD))
        self.signal_ttl = float(cfg.get("signal_ttl_seconds", DEFAULT_SIGNAL_TTL_S))
        self.cooldown = float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_S))

        mtb = cfg.get(CONFIG_MAX_TRADE_BASE)
        self.max_trade_base: Optional[Decimal] = to_decimal(mtb) if mtb is not None else None
        if self.max_trade_base is not None and self.max_trade_base <= 0:
            raise ValueError("max_trade_base must be positive when set")

        self.last_signal_time: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, pair: str, size: Optional[Any] = None) -> Optional[Signal]:
        """Try to produce a signal for ``pair``.

        When ``size`` is ``None`` (typical bot path), pick the **largest** feasible
        base size capped by ``max_position_usd``, inventory, and optional
        ``max_trade_base`` in config. With size-independent DEX stubs that is
        fee-optimal for linear PnL in notional; with :class:`PricingEngine`
        quotes, a short grid search maximizes :meth:`FeeStructure.net_profit_usd`.

        When ``size`` is set (tests / diagnostics), behaviour matches the legacy
        fixed-size path.
        """
        if "/" not in pair:
            raise ValueError(f"pair must be 'BASE/QUOTE', got {pair!r}")
        if size is None:
            return self._generate_optimal_size(pair)
        size_d = to_decimal(size)
        if size_d <= 0:
            raise ValueError("size must be positive")
        return self._generate_at_size(pair, size_d)

    def _generate_optimal_size(self, pair: str) -> Optional[Signal]:
        if self._in_cooldown(pair):
            return None
        probe = self._optimal_probe_base()
        prices = self._fetch_prices(pair, probe)
        if prices is None:
            self.last_signal_time[pair] = time.time()
            return None
        direction, spread_bps, cex_price, dex_price = self._best_direction(prices)
        if direction is None:
            return None
        cap = self._max_feasible_base_size(pair, direction, prices)
        if cap <= 0:
            return None
        if self._dex_quotes_size_dependent():
            chosen = self._best_base_size_on_grid(pair, direction, cap)
            if chosen is None or chosen <= 0:
                return None
        else:
            chosen = cap
        sig = self._generate_at_size(pair, chosen)
        if sig is not None:
            sig.metadata.setdefault("size_mode", "optimal")
        return sig

    def _optimal_probe_base(self) -> Decimal:
        if self.max_trade_base is not None:
            return min(DEFAULT_OPTIMAL_SIZE_PROBE_BASE, self.max_trade_base)
        return DEFAULT_OPTIMAL_SIZE_PROBE_BASE

    def _dex_quotes_size_dependent(self) -> bool:
        return self.pricing is not None and self.token_resolver is not None

    def _max_feasible_base_size(
        self,
        pair: str,
        direction: Direction,
        prices: dict[str, Decimal],
    ) -> Decimal:
        """Upper bound on base size from position limit + inventory (+ optional max_trade_base)."""
        base, quote = pair.split("/")
        cex_bid, cex_ask = prices["cex_bid"], prices["cex_ask"]
        dex_buy = prices["dex_buy"]
        if direction == Direction.BUY_CEX_SELL_DEX:
            cex_ref = cex_ask
            pos_cap = self.max_position_usd / cex_ref if cex_ref > 0 else Decimal("0")
            inv_cap = min(
                self.inventory.get_available(self.wallet_venue, base),
                self.inventory.get_available(self.cex_venue, quote)
                / (cex_ask * INVENTORY_SAFETY_MULT)
                if cex_ask > 0
                else Decimal("0"),
            )
        else:
            cex_ref = cex_bid
            pos_cap = self.max_position_usd / cex_ref if cex_ref > 0 else Decimal("0")
            inv_cap = min(
                self.inventory.get_available(self.cex_venue, base),
                self.inventory.get_available(self.wallet_venue, quote)
                / (dex_buy * INVENTORY_SAFETY_MULT)
                if dex_buy > 0
                else Decimal("0"),
            )
        cap = pos_cap if pos_cap <= inv_cap else inv_cap
        if self.max_trade_base is not None:
            cap = cap if cap <= self.max_trade_base else self.max_trade_base
        if cap <= 0:
            return Decimal("0")
        # Tie notional to max_position_usd so downstream ``within_limits`` never trips on rounding.
        cap = self._clamp_size_to_max_notional(cap, cex_ref)
        return cap if cap > 0 else Decimal("0")

    def _best_base_size_on_grid(
        self,
        pair: str,
        direction: Direction,
        cap: Decimal,
    ) -> Optional[Decimal]:
        """Pick base size in ``(0, cap]`` that maximizes net USD PnL (pricing path)."""
        best_s: Optional[Decimal] = None
        best_net = self.min_profit_usd - Decimal("1")
        seen: set[Decimal] = set()
        for i in range(1, OPTIMAL_SIZE_GRID_SAMPLES + 1):
            s = (cap * Decimal(i) / Decimal(OPTIMAL_SIZE_GRID_SAMPLES)).quantize(
                Decimal("0.00000001"),
                rounding=ROUND_DOWN,
            )
            if s <= 0 or s in seen:
                continue
            seen.add(s)
            cand = self._net_and_direction_at_size(pair, s)
            if cand is None:
                continue
            net, dir2, cpx = cand
            if dir2 != direction:
                continue
            if net > best_net:
                best_net = net
                best_s = s
        cap_q = cap.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if cap_q > 0 and cap_q not in seen:
            cand = self._net_and_direction_at_size(pair, cap_q)
            if cand is not None:
                net, dir2, _cpx = cand
                if dir2 == direction and net > best_net:
                    best_net = net
                    best_s = cap_q
        return best_s

    def _net_and_direction_at_size(
        self,
        pair: str,
        size_d: Decimal,
    ) -> Optional[tuple[Decimal, Direction, Decimal]]:
        pr = self._fetch_prices(pair, size_d)
        if pr is None:
            return None
        direction, spread_bps, cex_price, _dex_price = self._best_direction(pr)
        if direction is None:
            return None
        trade_value = size_d * cex_price
        if trade_value <= 0:
            return None
        net = self.fees.net_profit_usd(spread_bps, trade_value)
        if net < self.min_profit_usd:
            return None
        if not self._check_inventory(pair, direction, size_d, cex_price):
            return None
        if trade_value > self.max_position_usd:
            return None
        return net, direction, cex_price

    def _generate_at_size(self, pair: str, size_d: Decimal) -> Optional[Signal]:
        if self._in_cooldown(pair):
            return None

        prices = self._fetch_prices(pair, size_d)
        if prices is None:
            # Treat fetch failure as a cooldown-worthy event to avoid tight retry loops.
            self.last_signal_time[pair] = time.time()
            return None

        direction, spread_bps, cex_price, dex_price = self._best_direction(prices)
        if direction is None:
            return None

        trade_value = size_d * cex_price
        gross_pnl = spread_bps / BPS_DENOM * trade_value
        fees_usd = self.fees.total_fee_usd(trade_value)
        net_pnl = gross_pnl - fees_usd

        if net_pnl < self.min_profit_usd:
            return None

        inventory_ok = self._check_inventory(pair, direction, size_d, cex_price)
        within_limits = trade_value <= self.max_position_usd

        now = time.time()
        signal = Signal.create(
            pair=pair,
            direction=direction,
            cex_price=cex_price,
            dex_price=dex_price,
            spread_bps=spread_bps,
            size=size_d,
            expected_gross_pnl=gross_pnl,
            expected_fees=fees_usd,
            expected_net_pnl=net_pnl,
            score=Decimal("0"),
            expiry=now + self.signal_ttl,
            inventory_ok=inventory_ok,
            within_limits=within_limits,
        )

        self.last_signal_time[pair] = now
        return signal

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _clamp_size_to_max_notional(self, size_d: Decimal, cex_price: Decimal) -> Decimal:
        """Ensure ``size * cex_price`` never exceeds ``max_position_usd`` (Decimal drift)."""
        if cex_price <= 0 or size_d <= 0:
            return Decimal("0")
        max_sz = (self.max_position_usd / cex_price).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_DOWN,
        )
        return size_d if size_d <= max_sz else max_sz

    def _in_cooldown(self, pair: str) -> bool:
        last = self.last_signal_time.get(pair, 0.0)
        return (time.time() - last) < self.cooldown

    def _best_direction(
        self,
        prices: dict[str, Decimal],
    ) -> tuple[Optional[Direction], Decimal, Decimal, Decimal]:
        """Pick the more profitable direction given quoted bids/asks."""
        cex_bid, cex_ask = prices["cex_bid"], prices["cex_ask"]
        dex_buy, dex_sell = prices["dex_buy"], prices["dex_sell"]

        # buy CEX (pay ask), sell DEX (receive dex_sell): edge = dex_sell - cex_ask
        spread_a = (dex_sell - cex_ask) / cex_ask * BPS_DENOM if cex_ask > 0 else Decimal("0")
        # buy DEX (pay dex_buy), sell CEX (receive bid): edge = cex_bid - dex_buy
        spread_b = (cex_bid - dex_buy) / dex_buy * BPS_DENOM if dex_buy > 0 else Decimal("0")

        if spread_a >= spread_b and spread_a >= self.min_spread_bps:
            return Direction.BUY_CEX_SELL_DEX, spread_a, cex_ask, dex_sell
        if spread_b >= self.min_spread_bps:
            return Direction.BUY_DEX_SELL_CEX, spread_b, cex_bid, dex_buy
        return None, Decimal("0"), Decimal("0"), Decimal("0")

    def _fetch_prices(self, pair: str, size: Decimal) -> Optional[dict[str, Decimal]]:
        """Fetch CEX order book (Decimal) and DEX quotes; stub DEX if unavailable."""
        try:
            ob = self.exchange.fetch_order_book(pair)
            # fetch_order_book normalizes bids/asks to Decimal tuples already.
            if not ob.get("bids") or not ob.get("asks"):
                return None
            cex_bid = to_decimal(ob["bids"][0][0])
            cex_ask = to_decimal(ob["asks"][0][0])
        except Exception as exc:
            logger.warning("order book fetch failed for %s: %s", pair, exc)
            return None

        if cex_bid <= 0 or cex_ask <= 0:
            return None

        dex_buy, dex_sell = self._fetch_dex_prices(pair, size, cex_bid, cex_ask)
        if dex_buy is None or dex_sell is None or dex_buy <= 0 or dex_sell <= 0:
            return None
        return {
            "cex_bid": cex_bid,
            "cex_ask": cex_ask,
            "dex_buy": dex_buy,
            "dex_sell": dex_sell,
        }

    def _fetch_dex_prices(
        self,
        pair: str,
        size: Decimal,
        cex_bid: Decimal,
        cex_ask: Decimal,
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (dex_buy_price, dex_sell_price) as Decimals, or (None, None) on failure.

        If a :class:`PricingEngine` and a ``token_resolver`` are provided, use
        real on-chain quotes. Otherwise fall back to a clearly-marked stub that
        nudges DEX prices above CEX mid.
        """
        if self.pricing is not None and self.token_resolver is not None:
            try:
                base_tok, quote_tok = self.token_resolver(pair)
                amount_in_base = int(size * (Decimal(10) ** base_tok.decimals))
                gas_price_gwei = 1
                buy_quote = self.pricing.get_quote(
                    quote_tok,
                    base_tok,
                    amount_in_base,
                    gas_price_gwei,
                )
                sell_quote = self.pricing.get_quote(
                    base_tok,
                    quote_tok,
                    amount_in_base,
                    gas_price_gwei,
                )
                # price = quote paid/received per unit base
                dex_buy = to_decimal(buy_quote.gross_output) / (
                    size * Decimal(10) ** quote_tok.decimals
                )
                dex_sell = to_decimal(sell_quote.gross_output) / (
                    size * Decimal(10) ** quote_tok.decimals
                )
                return dex_buy, dex_sell
            except Exception as exc:
                logger.warning("pricing engine quote failed for %s: %s (using stub)", pair, exc)

        logger.debug("DEX STUB prices in use for %s", pair)
        mid = (cex_bid + cex_ask) / Decimal("2")
        return mid * STUB_DEX_BUY_PREMIUM, mid * STUB_DEX_SELL_PREMIUM

    def _check_inventory(
        self,
        pair: str,
        direction: Direction,
        size: Decimal,
        price: Decimal,
    ) -> bool:
        """Verify we have enough on each venue to execute both legs."""
        base, quote = pair.split("/")
        required_quote = size * price * INVENTORY_SAFETY_MULT
        if direction == Direction.BUY_CEX_SELL_DEX:
            # Need quote on CEX (to buy) and base on wallet (to deliver on DEX).
            have_quote = self.inventory.get_available(self.cex_venue, quote)
            have_base = self.inventory.get_available(self.wallet_venue, base)
            return have_quote >= required_quote and have_base >= size
        # BUY_DEX_SELL_CEX: need quote in wallet (to buy DEX) and base on CEX (to sell).
        have_quote = self.inventory.get_available(self.wallet_venue, quote)
        have_base = self.inventory.get_available(self.cex_venue, base)
        return have_quote >= required_quote and have_base >= size
