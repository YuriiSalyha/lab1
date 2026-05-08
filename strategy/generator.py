"""Signal generator: detect opportunity, validate fees, and check inventory.

Replaces and extends the detection-only :class:`scripts.arb_checker.ArbChecker`
with scoring, cooldown, inventory validation, and TTL-bound signals.

All calculations are performed in :class:`~decimal.Decimal`. The
:class:`ExchangeClient` already returns :class:`Decimal` prices; config values
are coerced with :func:`~strategy.signal.to_decimal` (string/int/Decimal, not
binary floats). Signal TTL / cooldown seconds are stored as ``Decimal`` and
converted to ``float`` only when combined with :func:`time.time` for OS
timestamps.
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
DEFAULT_SIGNAL_TTL_S = Decimal("5")
DEFAULT_COOLDOWN_S = Decimal("2")
# Probe base size when ``size`` is omitted (DEX quotes may depend on size).
DEFAULT_OPTIMAL_SIZE_PROBE_BASE = Decimal("0.01")
# Grid resolution when :class:`PricingEngine` quotes vary with trade size.
OPTIMAL_SIZE_GRID_SAMPLES = 12
# Floor on trade USD notional considered for sizing — avoids gas-dominated dust trades.
DEFAULT_MIN_TRADE_USD = Decimal("2")
# Iterations for the golden-section search over base size when pricing math
# is size-dependent. ~20 evals locate the optimum to <0.05% of cap on a
# unimodal net-PnL curve (fees flat, slippage convex), much tighter than the
# fixed 12-sample grid for the same number of evaluations.
GOLDEN_SECTION_ITERATIONS = 20
CONFIG_MAX_TRADE_BASE = "max_trade_base"
CONFIG_MIN_TRADE_USD = "min_trade_usd"
CONFIG_MAX_TRADE_USD = "max_trade_usd"
# Multiplicative safety margin when checking that quote balance covers the buy.
INVENTORY_SAFETY_MULT = Decimal("1.01")

# Callable: pair string -> (base_token_obj, quote_token_obj) for PricingEngine quotes.
TokenResolver = Callable[[str], tuple[Any, Any]]


class DexQuotesUnavailableError(RuntimeError):
    """Raised when on-chain DEX quotes cannot be produced (no engine, resolver, or pool math)."""


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
        # When live V2 math is wired (``pricing`` + ``token_resolver``), ``dex_buy`` /
        # ``dex_sell`` already include the pool's swap fee inside ``getAmountIn`` /
        # ``getAmountOut``. Adding ``dex_swap_bps`` again in ``FeeStructure.total_fee_bps``
        # would charge the same fee twice on the engine_math path, which inflates
        # losses (and shrinks net PnL) by ~30 bps × notional. Replace ``fees`` with
        # a copy that zeroes the DEX bps in that case while keeping CEX taker + gas.
        # When pricing is absent (tests, demo paths), keep the original fees.
        self.fees = (
            FeeStructure(
                cex_taker_bps=fee_structure.cex_taker_bps,
                dex_swap_bps=Decimal("0"),
                gas_cost_usd=fee_structure.gas_cost_usd,
            )
            if pricing_module is not None and token_resolver is not None
            else fee_structure
        )
        self.token_resolver = token_resolver
        self.cex_venue = cex_venue
        self.wallet_venue = wallet_venue

        cfg = config or {}
        self.min_spread_bps = to_decimal(cfg.get("min_spread_bps", DEFAULT_MIN_SPREAD_BPS))
        self.min_profit_usd = to_decimal(cfg.get("min_profit_usd", DEFAULT_MIN_PROFIT_USD))
        self.max_position_usd = to_decimal(cfg.get("max_position_usd", DEFAULT_MAX_POSITION_USD))
        self.signal_ttl = to_decimal(cfg.get("signal_ttl_seconds", DEFAULT_SIGNAL_TTL_S))
        self.cooldown = to_decimal(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN_S))

        mtb = cfg.get(CONFIG_MAX_TRADE_BASE)
        self.max_trade_base: Optional[Decimal] = to_decimal(mtb) if mtb is not None else None
        if self.max_trade_base is not None and self.max_trade_base <= 0:
            raise ValueError("max_trade_base must be positive when set")

        self.min_trade_usd: Decimal = to_decimal(
            cfg.get(CONFIG_MIN_TRADE_USD, DEFAULT_MIN_TRADE_USD)
        )
        if self.min_trade_usd < 0:
            raise ValueError("min_trade_usd must be non-negative when set")

        # Optional **upper** USD notional ceiling for any single trade. When set,
        # the size search clamps to ``min(max_position_usd, max_trade_usd)`` and
        # ``_validate_size_or_none`` rejects any candidate above it. Keeps the
        # bot operating in a narrow ``[min_trade_usd, max_trade_usd]`` band so
        # downstream risk (``risk/manager.py``) never has to reject after the
        # fact.
        mtu = cfg.get(CONFIG_MAX_TRADE_USD)
        self.max_trade_usd: Optional[Decimal] = to_decimal(mtu) if mtu is not None else None
        if self.max_trade_usd is not None and self.max_trade_usd <= 0:
            raise ValueError("max_trade_usd must be positive when set")
        if self.max_trade_usd is not None and self.max_trade_usd < self.min_trade_usd:
            raise ValueError(
                f"max_trade_usd ({self.max_trade_usd}) must be >= "
                f"min_trade_usd ({self.min_trade_usd})",
            )

        self.last_signal_time: dict[str, float] = {}
        # Last successful price snapshot per pair; populated by :meth:`_fetch_prices`.
        # Carries CEX bid/ask + sizes and DEX buy/sell prices so the bot can render
        # a per-tick console line even when no signal qualifies.
        self.last_snapshot: dict[str, dict[str, Any]] = {}
        # Why the last :meth:`generate` call returned ``None`` for this pair.
        # Lets the bot tell the operator "edge present but inventory blocked"
        # vs "no edge at all". Set inside :meth:`_generate_optimal_size` and
        # :meth:`_generate_at_size`; reset to ``None`` whenever a signal is
        # actually produced. Values used today:
        # ``in_cooldown``, ``fetch_failed``, ``no_edge``, ``inventory_blocked``,
        # ``below_min_profit``, ``score_below_min`` (set by the bot), ``ok``.
        self.last_reason: dict[str, str] = {}
        # Where the most recent DEX quote came from per pair (``engine_math``).
        self.last_dex_price_source: dict[str, str] = {}
        # Pool meta (kind / address / fee tier) for the most recent DEX quote per pair.
        # Plumbed into Signal.metadata so the executor can dispatch V2 vs V3 swap
        # calldata without re-resolving the pool.
        self.last_dex_pool_meta: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, pair: str, size: Optional[Any] = None) -> Optional[Signal]:
        """Try to produce a signal for ``pair``.

        When ``size`` is ``None`` (typical bot path), pick the **largest** feasible
        base size capped by ``max_position_usd``, inventory, and optional
        ``max_trade_base`` in config. With :class:`PricingEngine` quotes, a short
        grid search maximizes :meth:`FeeStructure.net_profit_usd`.

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
            self.last_reason[pair] = "in_cooldown"
            return None
        probe = self._optimal_probe_base()
        prices = self._fetch_prices(pair, probe)
        if prices is None:
            self.last_signal_time[pair] = time.time()
            self.last_reason[pair] = "fetch_failed"
            return None
        direction, _spread_probe, cex_price, _dex_price = self._best_direction(prices)
        if direction is None:
            self.last_reason[pair] = "no_edge"
            self._publish_hypothetical_net_pnl(pair, prices, probe, cex_price=None)
            return None
        cap = self._max_feasible_base_size(pair, direction, prices)
        size_for_pnl: Optional[Decimal] = None
        if cap > 0 and self._dex_quotes_size_dependent():
            best = self._best_base_size_search(pair, direction, cap)
            size_for_pnl = best
        elif cap > 0:
            size_for_pnl = cap

        # Surface the hypothetical net PnL on the snapshot for the console line,
        # even when no signal qualifies (below min profit, inventory blocked,
        # below min trade USD, etc.). Use the optimum-feasible size when we
        # have one, else the small probe size.
        self._publish_hypothetical_net_pnl(
            pair,
            prices,
            size_for_pnl if size_for_pnl is not None else probe,
            cex_price=cex_price,
        )

        if cap <= 0:
            self.last_reason[pair] = "inventory_blocked"
            return None
        if self._dex_quotes_size_dependent():
            if size_for_pnl is None or size_for_pnl <= 0:
                # Either inventory is too thin to clear min_profit_usd at any
                # size, or the engine quote keeps losing money — collapse both
                # under "below_min_profit" since the operator cannot tell the
                # difference at this layer.
                self.last_reason[pair] = "below_min_profit"
                return None
            chosen = size_for_pnl
        else:
            chosen = cap
        sig = self._generate_at_size(pair, chosen)
        if sig is not None:
            sig.metadata.setdefault("size_mode", "optimal")
            self.last_reason[pair] = "ok"
        return sig

    def _publish_hypothetical_net_pnl(
        self,
        pair: str,
        prices: dict[str, Decimal],
        size: Decimal,
        *,
        cex_price: Optional[Decimal],
    ) -> None:
        """Set ``last_snapshot[pair]['hypothetical_net_pnl_usd']`` for the console line.

        Uses execution-aware spread on the same pricing-engine math as
        ``_best_direction`` (so it reflects fee + slippage at ``size``) when
        a tradable direction exists; otherwise computes the better of the
        two no-edge directions for the given ``size`` (can be negative).
        """
        snap = self.last_snapshot.setdefault(pair, {})
        if size <= 0:
            return
        # Re-quote the DEX leg at the exact ``size`` so the displayed PnL is
        # what the trade would actually realize — pricing math is local
        # (no RPC), so this is fast.
        try:
            pr = self._fetch_prices_no_cex(pair, size, prices)
        except Exception:
            pr = prices
        cex_bid, cex_ask = pr["cex_bid"], pr["cex_ask"]
        dex_buy, dex_sell = pr["dex_buy"], pr["dex_sell"]
        if cex_ask <= 0 or dex_buy <= 0:
            return
        spread_a = (dex_sell - cex_ask) / cex_ask * BPS_DENOM
        spread_b = (cex_bid - dex_buy) / dex_buy * BPS_DENOM
        base_sym, _quote_sym = pair.split("/")
        if spread_a >= spread_b:
            cex_ref = cex_price if cex_price and cex_price > 0 else cex_ask
            spread_bps = spread_a
            # Buy CEX / sell DEX: ``size`` base is sold on the DEX leg.
            snap["hypothetical_sell_symbol"] = base_sym
            snap["hypothetical_sell_venue"] = "DEX"
        else:
            cex_ref = cex_price if cex_price and cex_price > 0 else cex_bid
            spread_bps = spread_b
            # Buy DEX / sell CEX: ``size`` base is sold on the CEX leg.
            snap["hypothetical_sell_symbol"] = base_sym
            snap["hypothetical_sell_venue"] = "CEX"
        tv = size * cex_ref
        if tv <= 0:
            return
        gross = spread_bps / BPS_DENOM * tv
        snap["hypothetical_net_pnl_usd"] = gross - self.fees.total_fee_usd(tv)
        snap["hypothetical_size_base"] = size
        snap["hypothetical_trade_value_usd"] = tv

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
        if self.max_trade_usd is not None and cex_ref > 0:
            cap_from_mtu = self.max_trade_usd / cex_ref
            if cap > cap_from_mtu:
                cap = cap_from_mtu
        if cap <= 0:
            return Decimal("0")
        # Tie notional to max_position_usd so downstream ``within_limits`` never trips on rounding.
        cap = self._clamp_size_to_max_notional(cap, cex_ref)
        return cap if cap > 0 else Decimal("0")

    def _best_base_size_search(
        self,
        pair: str,
        direction: Direction,
        cap: Decimal,
    ) -> Optional[Decimal]:
        """Pick base size in ``[s_min, cap]`` that maximizes net USD PnL.

        Uses **golden-section search** on the pricing-engine math (size-dependent
        leg, no CEX refetch — `_fetch_prices_no_cex`). Net PnL on Uniswap V2 is
        unimodal in trade size when fees are flat and slippage convex, so
        ~``GOLDEN_SECTION_ITERATIONS`` evaluations bracket the optimum tighter
        than the old fixed grid for the same cost.

        Constraints:
        - **`min_trade_usd`** floor (2 USD by default) so dust trades are skipped.
        - **`min_profit_usd`** filter on the chosen size.
        - **Inventory** + **`max_position_usd`** still enforced.
        """
        # Snapshot the order book once so every size eval is pure local math.
        baseline = self._fetch_prices(pair, self._optimal_probe_base())
        if baseline is None:
            return None
        cex_ref = (
            baseline["cex_ask"] if direction == Direction.BUY_CEX_SELL_DEX else baseline["cex_bid"]
        )
        if cex_ref <= 0:
            return None

        s_min = self._min_trade_base(cex_ref)
        if s_min <= 0:
            s_min = Decimal("0.00000001")
        cap_q = cap.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        s_min_q = s_min.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if s_min_q >= cap_q:
            # Sub-min cap: only chance is to evaluate cap itself.
            return self._validate_size_or_none(pair, direction, cap_q, baseline, cex_ref)

        cache: dict[Decimal, Optional[Decimal]] = {}

        def net_at(s: Decimal) -> Decimal:
            sq = s.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if sq <= 0:
                return Decimal("-1e18")
            if sq in cache:
                v = cache[sq]
                return v if v is not None else Decimal("-1e18")
            try:
                pr = self._fetch_prices_no_cex(pair, sq, baseline)
            except Exception:
                cache[sq] = None
                return Decimal("-1e18")
            dir2, spread_bps, _cpx, _dpx = self._best_direction(pr)
            if dir2 != direction:
                cache[sq] = None
                return Decimal("-1e18")
            tv = sq * cex_ref
            if tv <= 0:
                cache[sq] = None
                return Decimal("-1e18")
            net = self.fees.net_profit_usd(spread_bps, tv)
            cache[sq] = net
            return net

        # Golden-section on a unimodal target.
        phi = Decimal("0.61803398874989484820")
        a = s_min_q
        b = cap_q
        c = (b - (b - a) * phi).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        d = (a + (b - a) * phi).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        f_c = net_at(c)
        f_d = net_at(d)
        for _ in range(GOLDEN_SECTION_ITERATIONS):
            if f_c >= f_d:
                b, d, f_d = d, c, f_c
                c = (b - (b - a) * phi).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                if c <= a:
                    break
                f_c = net_at(c)
            else:
                a, c, f_c = c, d, f_d
                d = (a + (b - a) * phi).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                if d >= b:
                    break
                f_d = net_at(d)

        # Check the boundary and the four candidates from the final bracket.
        candidates = sorted({s_min_q, a, c, d, b, cap_q}, reverse=True)
        best_size: Optional[Decimal] = None
        best_net = self.min_profit_usd - Decimal("1")
        for s in candidates:
            sized = self._validate_size_or_none(pair, direction, s, baseline, cex_ref)
            if sized is None:
                continue
            net = net_at(s)
            if net > best_net:
                best_net = net
                best_size = sized
        return best_size

    def _validate_size_or_none(
        self,
        pair: str,
        direction: Direction,
        size: Decimal,
        baseline: dict[str, Decimal],
        cex_ref: Decimal,
    ) -> Optional[Decimal]:
        """Return ``size`` if it passes inventory + caps + min_profit_usd, else ``None``."""
        if size <= 0:
            return None
        try:
            pr = self._fetch_prices_no_cex(pair, size, baseline)
        except Exception:
            return None
        dir2, spread_bps, _cpx, _dpx = self._best_direction(pr)
        if dir2 != direction:
            return None
        tv = size * cex_ref
        if tv <= 0 or tv > self.max_position_usd:
            return None
        if self.max_trade_usd is not None and tv > self.max_trade_usd:
            return None
        if tv < self.min_trade_usd:
            return None
        net = self.fees.net_profit_usd(spread_bps, tv)
        if net < self.min_profit_usd:
            return None
        if not self._check_inventory(pair, direction, size, cex_ref):
            return None
        return size

    def _min_trade_base(self, cex_ref: Decimal) -> Decimal:
        """Floor on base size from ``min_trade_usd`` and the current price ref."""
        if self.min_trade_usd <= 0 or cex_ref <= 0:
            return Decimal("0")
        return self.min_trade_usd / cex_ref

    def _fetch_prices_no_cex(
        self,
        pair: str,
        size: Decimal,
        baseline: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        """Re-quote the DEX leg at ``size`` while reusing ``baseline`` CEX bid/ask.

        Used by the golden-section search and the hypothetical-PnL publisher to
        avoid re-pulling the order book per evaluation. Pricing-engine math is
        local (no RPC), so this is essentially free per call.
        """
        out = dict(baseline)
        if self.pricing is not None and self.token_resolver is not None:
            base_tok, quote_tok = self.token_resolver(pair)
            dex_buy, dex_sell, dex_spot = self.pricing.get_pair_prices_math(
                base_tok,
                quote_tok,
                size,
            )
            out["dex_buy"] = dex_buy
            out["dex_sell"] = dex_sell
            out["dex_spot"] = dex_spot
        return out

    def _generate_at_size(self, pair: str, size_d: Decimal) -> Optional[Signal]:
        if self._in_cooldown(pair):
            self.last_reason[pair] = "in_cooldown"
            return None

        prices = self._fetch_prices(pair, size_d)
        if prices is None:
            # Treat fetch failure as a cooldown-worthy event to avoid tight retry loops.
            self.last_signal_time[pair] = time.time()
            self.last_reason[pair] = "fetch_failed"
            return None

        direction, spread_bps, cex_price, dex_price = self._best_direction(prices)
        if direction is None:
            self.last_reason[pair] = "no_edge"
            return None

        trade_value = size_d * cex_price
        gross_pnl = spread_bps / BPS_DENOM * trade_value
        fees_usd = self.fees.total_fee_usd(trade_value)
        net_pnl = gross_pnl - fees_usd

        if net_pnl < self.min_profit_usd:
            self.last_reason[pair] = "below_min_profit"
            return None

        inventory_ok = self._check_inventory(pair, direction, size_d, cex_price)
        within_limits = trade_value <= self.max_position_usd and (
            self.max_trade_usd is None or trade_value <= self.max_trade_usd
        )

        now = time.time()
        meta_extra: dict[str, Any] = {}
        pool_meta = self.last_dex_pool_meta.get(pair)
        if pool_meta:
            meta_extra.update(pool_meta)
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
            expiry=now + float(self.signal_ttl),
            inventory_ok=inventory_ok,
            within_limits=within_limits,
            metadata=meta_extra,
        )

        self.last_signal_time[pair] = now
        self.last_reason[pair] = "ok"
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
        return (time.time() - last) < float(self.cooldown)

    def _spot_spread_meets_min(self, spread_bps: Decimal) -> bool:
        """True if spot edge vs CEX is at least ``min_spread_bps`` in magnitude.

        For positive ``min_spread_bps``, accept ``spread_bps >= m`` **or**
        ``spread_bps <= -m`` (e.g. ``m=10`` allows +10 bps and up, or −10 bps
        and down). Non-positive ``m`` keeps the legacy rule ``spread_bps >= m``.
        """
        m = self.min_spread_bps
        if m <= 0:
            return spread_bps >= m
        return spread_bps >= m or spread_bps <= -m

    def _best_direction(
        self,
        prices: dict[str, Decimal],
    ) -> tuple[Optional[Direction], Decimal, Decimal, Decimal]:
        """Pick direction using reserve spot vs CEX for min_spread_bps; return execution bps.

        Spot (``reserve_quote / reserve_base`` in human units) gates whether a
        marginal arb gap exists vs the CEX book. The spot gate uses
        :meth:`_spot_spread_meets_min` so a threshold of ``m`` bps allows
        ``|spot_edge| >= m`` when ``m > 0``. The returned ``spread_bps`` is the
        **execution** edge at the quoted ``base_size`` (``dex_buy`` /
        ``dex_sell``), used for PnL and :class:`Signal` so profit reflects
        slippage at the evaluated size.
        """
        cex_bid, cex_ask = prices["cex_bid"], prices["cex_ask"]
        dex_buy, dex_sell = prices["dex_buy"], prices["dex_sell"]
        dex_spot = prices["dex_spot"]

        if dex_spot <= 0 or cex_ask <= 0 or dex_buy <= 0:
            return None, Decimal("0"), Decimal("0"), Decimal("0")

        # Marginal / mid gap vs CEX (no probe slippage on the DEX reference).
        spread_buy_cex_sell_dex_spot = (dex_spot - cex_ask) / cex_ask * BPS_DENOM
        spread_buy_dex_sell_cex_spot = (cex_bid - dex_spot) / dex_spot * BPS_DENOM

        # Realized edge at this ``base_size`` (includes pool fee via getAmountIn/Out).
        spread_buy_cex_sell_dex_exec = (dex_sell - cex_ask) / cex_ask * BPS_DENOM
        spread_buy_dex_sell_cex_exec = (cex_bid - dex_buy) / dex_buy * BPS_DENOM

        a_spot, b_spot = spread_buy_cex_sell_dex_spot, spread_buy_dex_sell_cex_spot
        a_ok = self._spot_spread_meets_min(a_spot)
        b_ok = self._spot_spread_meets_min(b_spot)

        if a_spot >= b_spot and a_ok:
            return (
                Direction.BUY_CEX_SELL_DEX,
                spread_buy_cex_sell_dex_exec,
                cex_ask,
                dex_sell,
            )
        if b_ok:
            return (
                Direction.BUY_DEX_SELL_CEX,
                spread_buy_dex_sell_cex_exec,
                cex_bid,
                dex_buy,
            )
        if a_ok:
            return (
                Direction.BUY_CEX_SELL_DEX,
                spread_buy_cex_sell_dex_exec,
                cex_ask,
                dex_sell,
            )
        return None, Decimal("0"), Decimal("0"), Decimal("0")

    def _fetch_prices(self, pair: str, size: Decimal) -> Optional[dict[str, Decimal]]:
        """Fetch CEX order book and on-chain DEX math quotes (buy / sell / spot).

        On success, publishes the snapshot on :attr:`last_snapshot`. DEX fields
        require :meth:`_fetch_dex_prices` (no stub). If the order book is
        missing or invalid, returns ``None`` (soft failure). If the book is OK
        but DEX math is unavailable, raises :exc:`DexQuotesUnavailableError`.
        """
        try:
            ob = self.exchange.fetch_order_book(pair)
            # fetch_order_book normalizes bids/asks to Decimal tuples already.
            if not ob.get("bids") or not ob.get("asks"):
                return None
            cex_bid_row = ob["bids"][0]
            cex_ask_row = ob["asks"][0]
            cex_bid = to_decimal(cex_bid_row[0])
            cex_ask = to_decimal(cex_ask_row[0])
            cex_bid_size = to_decimal(cex_bid_row[1]) if len(cex_bid_row) > 1 else Decimal("0")
            cex_ask_size = to_decimal(cex_ask_row[1]) if len(cex_ask_row) > 1 else Decimal("0")
        except Exception as exc:
            logger.warning("order book fetch failed for %s: %s", pair, exc)
            return None

        if cex_bid <= 0 or cex_ask <= 0:
            return None

        dex_buy, dex_sell, dex_spot = self._fetch_dex_prices(pair, size, cex_bid, cex_ask)
        if dex_buy <= 0 or dex_sell <= 0 or dex_spot <= 0:
            return None

        cex_mid = (cex_bid + cex_ask) / Decimal("2")
        cex_spread_bps = (cex_ask - cex_bid) / cex_mid * BPS_DENOM if cex_mid > 0 else Decimal("0")
        dex_source = self.last_dex_price_source.get(pair, "engine_math")
        self.last_snapshot[pair] = {
            "cex_bid": cex_bid,
            "cex_ask": cex_ask,
            "cex_bid_size": cex_bid_size,
            "cex_ask_size": cex_ask_size,
            "cex_mid": cex_mid,
            "cex_spread_bps": cex_spread_bps,
            "dex_buy": dex_buy,
            "dex_sell": dex_sell,
            "dex_spot": dex_spot,
            "dex_source": dex_source,
            "size_probe_base": to_decimal(size),
            "fetched_at": time.time(),
        }
        return {
            "cex_bid": cex_bid,
            "cex_ask": cex_ask,
            "dex_buy": dex_buy,
            "dex_sell": dex_sell,
            "dex_spot": dex_spot,
        }

    def _fetch_dex_prices(
        self,
        pair: str,
        size: Decimal,
        cex_bid: Decimal,
        cex_ask: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return ``(dex_buy, dex_sell, dex_spot)`` from V2 pool math.

        Requires a :class:`~pricing.pricing_engine.PricingEngine` and
        ``token_resolver``. There is **no** CEX-mid stub fallback: misconfiguration
        or missing pools raise :exc:`DexQuotesUnavailableError` (or the engine's
        :exc:`~pricing.pricing_engine.QuoteError`).

        ``cex_bid`` / ``cex_ask`` are unused here but kept so demo / monkeypatch
        hooks match the same signature.
        """
        del cex_bid, cex_ask
        if self.pricing is None or self.token_resolver is None:
            raise DexQuotesUnavailableError(
                "DEX quotes require a PricingEngine and token_resolver "
                "(RPC + V2 pool addresses + resolver wiring). Stub fallback is disabled.",
            )
        base_tok, quote_tok = self.token_resolver(pair)
        # Use the pool-aware variant so we can plumb dex_kind / pool_address / fee_tier
        # into the generated Signal metadata, letting the executor dispatch V2/V3 swap
        # calldata without re-resolving the pool. Fall back to the 3-tuple variant for
        # mocked pricing engines / older callers that only expose ``get_pair_prices_math``.
        with_pool = getattr(self.pricing, "get_pair_prices_math_with_pool", None)
        result = with_pool(base_tok, quote_tok, size) if callable(with_pool) else None
        if isinstance(result, tuple) and len(result) == 5:
            dex_buy, dex_sell, dex_spot, pool, kind = result
            fee_tier = int(getattr(pool, "fee", 0)) if kind == "v3" else 0
            self.last_dex_pool_meta[pair] = {
                "dex_kind": kind,
                "pool_address": pool.address.checksum,
                "fee_tier": fee_tier,
            }
            self.last_dex_price_source[pair] = f"engine_math_{kind}"
        else:
            dex_buy, dex_sell, dex_spot = self.pricing.get_pair_prices_math(
                base_tok,
                quote_tok,
                size,
            )
            self.last_dex_price_source[pair] = "engine_math"
        return dex_buy, dex_sell, dex_spot

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
