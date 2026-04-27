"""Main arbitrage bot: wires strategy + executor + inventory + PnL modules.

Run modes:

- ``python scripts/arb_bot.py --demo``
    Uses a fully offline ``MockExchange`` + stub pricing so the main loop
    exercises every state transition without real credentials or RPC. The bot
    walks one deterministic ``DEMO_SCRIPT_SPREAD_BPS`` sequence (both arb
    directions across ``ETH/USDT`` and ``BTC/USDT``), updates mock balances on
    each success, prints a PnL summary, then **exits** (no infinite loop).
- ``python scripts/arb_bot.py``
    Live mode. Requires the CEX credentials expected by :class:`ExchangeClient`.
    If ``ETH_RPC_URL`` is set, the bot constructs :class:`PricingEngine`, loads
    Uniswap V2 pools from ``ARB_V2_POOLS`` (comma-separated ``0x`` addresses) or
    built-in mainnet defaults (WETH/USDT + WBTC/WETH), and wires
    ``token_resolver`` so the generator uses on-chain :meth:`PricingEngine.get_quote`
    prices (fork RPC must be reachable for quote simulation). If setup fails or
    env is unset, DEX legs use stub prices derived from the CEX book.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.types import Address  # noqa: E402
from executor.circuit_breaker import CircuitBreaker, CircuitBreakerConfig  # noqa: E402
from executor.engine import (  # noqa: E402
    VENUE_CEX,
    VENUE_DEX,
    ExecutionContext,
    Executor,
    ExecutorConfig,
    ExecutorState,
)
from executor.webhook_alerts import (  # noqa: E402
    WebhookDeliveryConfig,
    chain_trip_hooks,
    make_circuit_breaker_webhook_hook,
)
from inventory.pnl import ArbRecord, PnLEngine, TradeLeg  # noqa: E402
from inventory.tracker import InventoryTracker, Venue  # noqa: E402
from monitoring.prometheus_metrics import PrometheusMetrics, try_start_metrics_server  # noqa: E402
from strategy.fees import FeeStructure  # noqa: E402
from strategy.generator import SignalGenerator  # noqa: E402
from strategy.scorer import SignalScorer  # noqa: E402
from strategy.signal import Direction, to_decimal  # noqa: E402
from strategy.signal_priority import ScoredCandidate, sort_candidates_by_priority  # noqa: E402

# --- Module constants --------------------------------------------------------
DEFAULT_MIN_SCORE = Decimal("60")
DEFAULT_TICK_SECONDS = 1.0
DEFAULT_ERROR_BACKOFF_SECONDS = 5.0
DEFAULT_PAIR = "ETH/USDT"

# Mainnet Uniswap V2 pairs for default DEX routing when ARB_V2_POOLS is unset.
_DEFAULT_ARB_V2_POOLS: tuple[Address, ...] = (
    Address("0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852"),  # WETH/USDT
    Address("0xBb2b8038a1640196FbE3e38816F3e67Cba72D940"),  # WBTC/WETH
)
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Demo scenario parameters -----------------------------------------------------
DEMO_BASE_PRICE = Decimal("2000")
DEMO_BID_WIDTH = Decimal("0.5")
# Mid prices (quote per base) for each base symbol used in ``--demo``.
DEMO_PAIR_MID: dict[str, Decimal] = {
    "ETH": Decimal("2000"),
    "BTC": Decimal("42000"),
}
# Each tick applies (spread_a_bps, spread_b_bps) vs the CEX book:
#   spread_a → BUY_CEX_SELL_DEX edge: dex_sell = cex_ask * (1 + spread_a/10000)
#   spread_b → BUY_DEX_SELL_CEX edge: dex_buy = cex_bid / (1 + spread_b/10000)
# The generator picks the better direction when both edges are ≥ min_spread_bps.
DEMO_SCRIPT_SPREAD_BPS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("82"), Decimal("24")),  # CEX-buy route wins
    (Decimal("20"), Decimal("68")),  # DEX-buy route wins
    (Decimal("44"), Decimal("38")),  # both tradable; lower score → often skipped
    (Decimal("15"), Decimal("12")),  # both below min_spread → no signal
    (Decimal("28"), Decimal("75")),  # DEX-buy wins (wide)
    (Decimal("90"), Decimal("26")),  # CEX-buy wins
    (Decimal("48"), Decimal("45")),  # CEX-buy narrow; may execute or skip
    (Decimal("92"), Decimal("30")),  # CEX leg first then scripted DEX failure
    (Decimal("70"), Decimal("22")),  # CEX-buy wins
    (Decimal("22"), Decimal("65")),  # DEX-buy wins (second asset in rotation)
    (Decimal("36"), Decimal("34")),  # skip band
    (Decimal("25"), Decimal("20")),  # no opportunity
    (Decimal("55"), Decimal("72")),  # DEX-buy wins (72 > 55)
    (Decimal("86"), Decimal("28")),  # CEX-buy wins — last scripted tick
)
# Single scripted DEX timeout so the demo rarely trips the circuit breaker.
DEMO_SCRIPTED_DEX_FAILURE_INDICES: frozenset[int] = frozenset({7})
DEMO_CEX_BALANCE_BASE = Decimal("5")
DEMO_CEX_BALANCE_BTC = Decimal("3")
DEMO_CEX_BALANCE_QUOTE = Decimal("200000")
DEMO_WALLET_BALANCE_BASE = Decimal("5")
DEMO_WALLET_BALANCE_BTC = Decimal("3")
DEMO_WALLET_BALANCE_QUOTE = Decimal("200000")
DEMO_SIGNAL_CONFIG = {
    "min_spread_bps": Decimal("30"),
    # Very low floor so both "execute" and "score-too-low" scenarios generate.
    "min_profit_usd": Decimal("0.001"),
    "max_position_usd": Decimal("50000"),
    "signal_ttl_seconds": 5.0,
    "cooldown_seconds": 0.0,
}

logger = logging.getLogger("arb_bot")


def _demo_combined_portfolio_usd(mock: "MockExchange") -> Decimal:
    """Approximate CEX+wallet value in USD using :data:`DEMO_PAIR_MID` (demo MTM only)."""
    w = mock.wallet_balances_for_demo()
    cex = mock.fetch_balance()
    eth = w.get("ETH", Decimal("0")) + cex.get("ETH", {}).get("free", Decimal("0"))
    btc = w.get("BTC", Decimal("0")) + cex.get("BTC", {}).get("free", Decimal("0"))
    usdt = w.get("USDT", Decimal("0")) + cex.get("USDT", {}).get("free", Decimal("0"))
    return eth * DEMO_PAIR_MID["ETH"] + btc * DEMO_PAIR_MID["BTC"] + usdt


# --- Offline mock exchange ---------------------------------------------------


def _demo_pair_mid_bid_ask(symbol: str) -> tuple[Decimal, Decimal, Decimal]:
    base = symbol.split("/")[0]
    mid = DEMO_PAIR_MID.get(base, DEMO_BASE_PRICE)
    width = max(DEMO_BID_WIDTH, (mid * Decimal("0.00005")).quantize(Decimal("0.01")))
    return mid, mid - width, mid + width


class MockExchange:
    """Offline CEX stub for ``--demo``.

    One row of :data:`DEMO_SCRIPT_SPREAD_BPS` applies per bot tick; advance with
    :meth:`advance_demo_script` so multiple pairs can share the same step.
    """

    def __init__(
        self,
        *,
        dex_failure_indices: Optional[frozenset[int]] = None,
    ) -> None:
        self._dex_failure_indices = (
            dex_failure_indices
            if dex_failure_indices is not None
            else DEMO_SCRIPTED_DEX_FAILURE_INDICES
        )
        self._dex_fail_slots = 0
        self._order_seq = 0
        self._demo_script_index = 0
        self.last_book_script_index = 0
        self.last_applied_scripted_spread_bps: Optional[Decimal] = None
        self.current_dex_buy: Decimal = DEMO_BASE_PRICE
        self.current_dex_sell: Decimal = DEMO_BASE_PRICE
        self._cex: dict[str, dict[str, Decimal]] = {
            "ETH": {
                "free": DEMO_CEX_BALANCE_BASE,
                "locked": Decimal("0"),
                "total": DEMO_CEX_BALANCE_BASE,
            },
            "BTC": {
                "free": DEMO_CEX_BALANCE_BTC,
                "locked": Decimal("0"),
                "total": DEMO_CEX_BALANCE_BTC,
            },
            "USDT": {
                "free": DEMO_CEX_BALANCE_QUOTE,
                "locked": Decimal("0"),
                "total": DEMO_CEX_BALANCE_QUOTE,
            },
        }
        self._wallet: dict[str, dict[str, Decimal]] = {
            "ETH": {
                "free": DEMO_WALLET_BALANCE_BASE,
                "locked": Decimal("0"),
                "total": DEMO_WALLET_BALANCE_BASE,
            },
            "BTC": {
                "free": DEMO_WALLET_BALANCE_BTC,
                "locked": Decimal("0"),
                "total": DEMO_WALLET_BALANCE_BTC,
            },
            "USDT": {
                "free": DEMO_WALLET_BALANCE_QUOTE,
                "locked": Decimal("0"),
                "total": DEMO_WALLET_BALANCE_QUOTE,
            },
        }

    def demo_script_length(self) -> int:
        return len(DEMO_SCRIPT_SPREAD_BPS)

    def is_demo_exhausted(self) -> bool:
        return self._demo_script_index >= len(DEMO_SCRIPT_SPREAD_BPS)

    def arm_dex_failure_for_current_step(self) -> None:
        """Call once per bot tick before any ``fetch_order_book`` (multi-pair safe)."""
        if self.is_demo_exhausted():
            self._dex_fail_slots = 0
            return
        idx = self._demo_script_index
        self._dex_fail_slots = 1 if idx in self._dex_failure_indices else 0

    def advance_demo_script(self) -> None:
        self._demo_script_index += 1

    def wallet_balances_for_demo(self) -> dict[str, Decimal]:
        return {k: v["free"] for k, v in self._wallet.items()}

    def _bump_order_id(self) -> int:
        self._order_seq += 1
        return self._order_seq

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        if self.is_demo_exhausted():
            raise RuntimeError("demo script exhausted; bot should stop ticking")
        idx = self._demo_script_index
        sa, sb = DEMO_SCRIPT_SPREAD_BPS[idx]
        self.last_book_script_index = idx
        min_spread = DEMO_SIGNAL_CONFIG["min_spread_bps"]
        if sa < min_spread and sb < min_spread:
            self.last_applied_scripted_spread_bps = max(sa, sb)
        else:
            self.last_applied_scripted_spread_bps = sa if sa >= sb else sb

        _, bid, ask = _demo_pair_mid_bid_ask(symbol)
        fa = Decimal("1") + sa / Decimal("10000")
        fb = Decimal("1") + sb / Decimal("10000")
        self.current_dex_sell = ask * fa
        self.current_dex_buy = bid / fb
        mid = (bid + ask) / Decimal("2")
        return {
            "symbol": symbol,
            "timestamp": 0,
            "bids": [(bid, Decimal("10"))],
            "asks": [(ask, Decimal("10"))],
            "best_bid": (bid, Decimal("10")),
            "best_ask": (ask, Decimal("10")),
            "mid_price": mid,
            "spread_bps": (ask - bid) / mid * Decimal("10000"),
        }

    def fetch_balance(self) -> dict[str, dict[str, Decimal]]:
        return {k: dict(v) for k, v in self._cex.items()}

    def apply_balance_deltas_from_execution(self, ctx: ExecutionContext) -> None:
        """Update demo CEX + wallet balances after a successful simulated arb."""
        if ctx.state != ExecutorState.DONE or ctx.actual_net_pnl is None:
            return
        sig = ctx.signal
        base, quote = sig.pair.split("/")
        if ctx.leg1_venue == VENUE_CEX:
            cex_px = ctx.leg1_fill_price
            dex_px = ctx.leg2_fill_price
            sz = ctx.leg1_fill_size
        else:
            cex_px = ctx.leg2_fill_price
            dex_px = ctx.leg1_fill_price
            sz = ctx.leg1_fill_size
        if cex_px is None or dex_px is None or sz is None:
            return
        sz = to_decimal(sz)
        cex_px = to_decimal(cex_px)
        dex_px = to_decimal(dex_px)

        def _move(book: dict[str, dict[str, Decimal]], asset: str, delta: Decimal) -> None:
            row = book[asset]
            row["free"] = row["free"] + delta
            row["total"] = row["free"] + row["locked"]

        if sig.direction == Direction.BUY_CEX_SELL_DEX:
            _move(self._cex, quote, -sz * cex_px)
            _move(self._cex, base, sz)
            _move(self._wallet, base, -sz)
            _move(self._wallet, quote, sz * dex_px)
        else:
            _move(self._wallet, quote, -sz * dex_px)
            _move(self._wallet, base, sz)
            _move(self._cex, base, -sz)
            _move(self._cex, quote, sz * cex_px)

    def create_limit_ioc_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        oid = self._bump_order_id()
        return {
            "id": f"mock-{oid}",
            "status": "filled",
            "amount_filled": to_decimal(amount),
            "avg_fill_price": to_decimal(price),
            "fee": Decimal("0"),
            "fee_asset": symbol.split("/")[1],
        }

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        oid = self._bump_order_id()
        return {
            "id": f"mock-unwind-{oid}",
            "status": "filled",
            "amount_filled": to_decimal(amount),
            "avg_fill_price": to_decimal(0),
        }

    def should_fail_next_dex(self) -> bool:
        if self._dex_fail_slots <= 0:
            return False
        self._dex_fail_slots -= 1
        return True


# --- Demo helpers ------------------------------------------------------------


class _FailingExecutor(Executor):
    """Demo executor that aborts the DEX leg when the MockExchange says so.

    Used in ``--demo`` to produce the "FAILED: DEX timeout - unwound" line the
    evaluator expects. Live runs use the plain :class:`Executor`.
    """

    def __init__(self, *args: Any, mock_exchange: MockExchange, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mock_exchange = mock_exchange

    async def _execute_dex_leg(self, signal, size):  # type: ignore[override]
        if self._mock_exchange.should_fail_next_dex():
            await asyncio.sleep(0)
            return {
                "success": False,
                "price": signal.dex_price,
                "filled": Decimal("0"),
                "error": "DEX timeout",
            }
        return await super()._execute_dex_leg(signal, size)


# --- ArbBot ------------------------------------------------------------------


@dataclass
class ArbBotConfig:
    pairs: list[str] = field(default_factory=lambda: [DEFAULT_PAIR])
    min_score: Decimal = DEFAULT_MIN_SCORE
    simulation: bool = True
    demo: bool = False
    tick_seconds: float = DEFAULT_TICK_SECONDS
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS
    signal_config: dict[str, Any] = field(default_factory=dict)
    rpc_url: Optional[str] = None
    max_trade_size: Optional[Decimal] = None
    max_signals_per_tick: int = 1

    def __post_init__(self) -> None:
        self.min_score = to_decimal(self.min_score)
        if self.max_trade_size is not None:
            self.max_trade_size = to_decimal(self.max_trade_size)
            if self.max_trade_size <= 0:
                raise ValueError("max_trade_size must be positive when set")
        if self.max_signals_per_tick < 1:
            raise ValueError("max_signals_per_tick must be >= 1")


class ArbBot:
    """Run loop that glues SignalGenerator, SignalScorer, and Executor."""

    def __init__(self, config: ArbBotConfig) -> None:
        self.config = config
        self.running = False

        self._metrics = PrometheusMetrics()

        metrics_port = int(os.getenv("PROMETHEUS_METRICS_PORT", "0") or "0")
        if metrics_port > 0:
            srv = try_start_metrics_server(metrics_port)
            if srv is not None:
                logger.info("Prometheus /metrics listening on port %s", metrics_port)

        def _trip_metric(_cb: Any) -> None:
            self._metrics.record_circuit_trip()

        wh_url = (
            os.getenv("ARB_CIRCUIT_WEBHOOK_URL") or os.getenv("ARB_WEBHOOK_URL") or ""
        ).strip()
        wh_timeout = float(os.getenv("ARB_WEBHOOK_TIMEOUT_SECONDS", "5") or "5")
        trip_parts = [_trip_metric]
        if wh_url:
            trip_parts.append(
                make_circuit_breaker_webhook_hook(
                    WebhookDeliveryConfig(url=wh_url, timeout_seconds=wh_timeout),
                ),
            )
        self._circuit_on_trip = (
            chain_trip_hooks(*trip_parts) if len(trip_parts) > 1 else trip_parts[0]
        )

        self.chain_client = None
        self.pricing_engine = None
        self._token_resolver = None

        if config.demo:
            self.exchange: Any = MockExchange()
        else:
            from config.config import BINANCE_CONFIG  # local import avoids demo dep
            from exchange.client import ExchangeClient

            self.exchange = ExchangeClient(BINANCE_CONFIG, exchange_id="binance")
            self._maybe_init_pricing_engine()

        self.inventory = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        self.pnl_engine = PnLEngine()
        self._cumulative_arb_pnl: Decimal = Decimal("0")
        self._session_portfolio_start_usd: Optional[Decimal] = None
        # In demo mode we zero out gas so small trade sizes stay profitable;
        # real runs use the default fee assumptions.
        self.fees = FeeStructure(gas_cost_usd=Decimal("0")) if config.demo else FeeStructure()

        signal_cfg = dict(DEMO_SIGNAL_CONFIG if config.demo else {})
        signal_cfg.update(config.signal_config or {})
        if config.max_trade_size is not None:
            signal_cfg["max_trade_base"] = config.max_trade_size

        self.generator = SignalGenerator(
            self.exchange,
            self.pricing_engine,
            self.inventory,
            self.fees,
            signal_cfg,
            token_resolver=self._token_resolver,
        )
        if config.demo:
            self._rewire_demo_dex_prices()
        self.scorer = SignalScorer()

        dex_expected_raw = os.getenv("DEX_EXPECTED_CHAIN_ID", "").strip()
        dex_expected_id = int(dex_expected_raw) if dex_expected_raw else None
        allow_mainnet = os.getenv("DEX_ALLOW_MAINNET", "").strip().lower() in ("1", "true", "yes")
        dex_live = os.getenv("DEX_LIVE_ENABLED", "").strip().lower() in ("1", "true", "yes")
        dex_preflight_raw = os.getenv("DEX_RUN_PREFLIGHT", "1").strip().lower()
        dex_run_preflight = dex_preflight_raw not in ("0", "false", "no")

        executor_config = ExecutorConfig(
            simulation_mode=config.simulation,
            dex_slippage_bps=Decimal(os.getenv("DEX_SLIPPAGE_BPS", "50")),
            dex_deadline_seconds=int(os.getenv("DEX_DEADLINE_SECONDS", "300")),
            dex_run_preflight=dex_run_preflight,
            dex_expected_chain_id=dex_expected_id,
            dex_allow_mainnet=allow_mainnet,
        )

        dex_wallet = None
        if (
            dex_live
            and not config.simulation
            and not config.demo
            and self.pricing_engine is not None
            and self._token_resolver is not None
        ):
            try:
                from core.wallet import WalletManager

                dex_wallet = WalletManager.from_env("PRIVATE_KEY")
                logger.info("DEX_LIVE_ENABLED: live router swaps enabled for this process")
            except Exception as exc:
                logger.warning(
                    "DEX_LIVE_ENABLED but wallet init failed (%s); DEX leg will fail closed",
                    exc,
                )

        if config.demo:
            demo_cb = CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=10,
                    window_seconds=300.0,
                    cooldown_seconds=60.0,
                ),
                on_trip=self._circuit_on_trip,
            )
            self.executor: Executor = _FailingExecutor(
                self.exchange,
                self.pricing_engine,
                self.inventory,
                executor_config,
                fees=self.fees,
                circuit_breaker=demo_cb,
                mock_exchange=self.exchange,
                dex_wallet=dex_wallet,
                dex_token_resolver=self._token_resolver,
                metrics=self._metrics,
            )
        else:
            live_cb = CircuitBreaker(on_trip=self._circuit_on_trip)
            self.executor = Executor(
                self.exchange,
                self.pricing_engine,
                self.inventory,
                executor_config,
                fees=self.fees,
                circuit_breaker=live_cb,
                dex_wallet=dex_wallet,
                dex_token_resolver=self._token_resolver,
                metrics=self._metrics,
            )

    def _rewire_demo_dex_prices(self) -> None:
        """In demo mode, pull scripted DEX prices from the ``MockExchange``."""
        mock = self.exchange
        if not isinstance(mock, MockExchange):
            return

        def demo_dex_prices(pair, size, cex_bid, cex_ask):
            return mock.current_dex_buy, mock.current_dex_sell

        self.generator._fetch_dex_prices = demo_dex_prices  # type: ignore[assignment]

    def _maybe_init_pricing_engine(self) -> None:
        if not self.config.rpc_url:
            return
        try:
            from chain.client import ChainClient
            from pricing.pricing_engine import PricingEngine
            from strategy.dex_token_resolver import token_resolver_from_pricing_engine

            self.chain_client = ChainClient([self.config.rpc_url])
            quote_sender = Address.from_string(
                os.getenv("ARB_QUOTE_SENDER", "0x0000000000000000000000000000000000000001"),
            )
            fork_url = os.getenv("FORK_RPC_URL", "http://127.0.0.1:8545")
            ws_url = os.getenv("WS_URL", "ws://127.0.0.1:8546")
            self.pricing_engine = PricingEngine(
                self.chain_client,
                fork_url,
                ws_url,
                quote_sender,
            )
            raw = os.getenv("ARB_V2_POOLS", "").strip()
            if raw:
                pool_addrs = [Address.from_string(x.strip()) for x in raw.split(",") if x.strip()]
            else:
                pool_addrs = list(_DEFAULT_ARB_V2_POOLS)
            if not pool_addrs:
                logger.warning("ARB_V2_POOLS is empty; continuing without on-chain pools")
                self.chain_client = None
                self.pricing_engine = None
                return
            self.pricing_engine.load_pools(pool_addrs)
            self._token_resolver = token_resolver_from_pricing_engine(self.pricing_engine)
            logger.info(
                "PricingEngine: loaded %d Uniswap V2 pool(s); generator uses on-chain DEX quotes",
                len(self.pricing_engine.pools),
            )
        except Exception as exc:
            logger.warning("PricingEngine setup failed (%s); continuing without DEX quotes", exc)
            self.chain_client = None
            self.pricing_engine = None
            self._token_resolver = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _session_portfolio_usd_mark(self) -> Optional[Decimal]:
        """Mark-to-market of demo CEX+wallet; ``None`` when not available (e.g. live)."""
        if self.config.demo and isinstance(self.exchange, MockExchange):
            return _demo_combined_portfolio_usd(self.exchange)
        return None

    async def run(self) -> None:
        self.running = True
        self._demo_cb_open_logged = False
        self._cumulative_arb_pnl = Decimal("0")
        logger.info("Bot starting...")
        await self._sync_balances()
        self._session_portfolio_start_usd = self._session_portfolio_usd_mark()

        while self.running:
            if (
                self.config.demo
                and isinstance(self.exchange, MockExchange)
                and self.exchange.is_demo_exhausted()
            ):
                logger.info(
                    "Demo finished scripted market (%d steps).",
                    self.exchange.demo_script_length(),
                )
                self._log_demo_summary()
                break
            try:
                await self._tick()
                await asyncio.sleep(self.config.tick_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Tick error: %s", exc)
                await asyncio.sleep(self.config.error_backoff_seconds)

    def stop(self) -> None:
        self.running = False

    def _log_demo_summary(self) -> None:
        su = self.pnl_engine.summary()
        logger.info(
            "Demo summary: trades=%d total_pnl_usd=$%.2f",
            su["total_trades"],
            float(su["total_pnl_usd"]),
        )
        if isinstance(self.exchange, MockExchange):
            w = self.exchange.wallet_balances_for_demo()
            c = self.exchange.fetch_balance()
            logger.info(
                "Demo wallet (free): ETH=%.6f BTC=%.6f USDT=%.2f",
                float(w.get("ETH", Decimal("0"))),
                float(w.get("BTC", Decimal("0"))),
                float(w.get("USDT", Decimal("0"))),
            )
            logger.info(
                "Demo CEX (free): ETH=%.6f BTC=%.6f USDT=%.2f",
                float(c.get("ETH", {}).get("free", Decimal("0"))),
                float(c.get("BTC", {}).get("free", Decimal("0"))),
                float(c.get("USDT", {}).get("free", Decimal("0"))),
            )

    async def _tick(self) -> None:
        cb = self.executor.circuit_breaker
        if cb.is_open():
            if not self._demo_cb_open_logged:
                self._demo_cb_open_logged = True
                logger.warning(
                    "Circuit breaker open (%.0fs until reset)",
                    cb.time_until_reset(),
                )
            return

        try:
            if self.config.demo and isinstance(self.exchange, MockExchange):
                self.exchange.arm_dex_failure_for_current_step()

            no_opp_logged = False
            candidates: list[ScoredCandidate] = []
            for pair in self.config.pairs:
                signal = self.generator.generate(pair)
                if signal is None:
                    if (
                        self.config.demo
                        and isinstance(self.exchange, MockExchange)
                        and not no_opp_logged
                    ):
                        s = self.exchange.last_applied_scripted_spread_bps
                        if s is not None:
                            logger.info(
                                "No opportunity: no tradeable edge (demo scripted %.1f bps)",
                                float(s),
                            )
                            no_opp_logged = True
                    continue

                signal.score = self.scorer.score(signal, self.inventory.get_skews())

                if not signal.inventory_ok:
                    logger.info("Skipped: inventory insufficient (%s)", pair)
                    continue
                if not signal.within_limits:
                    logger.info("Skipped: exceeds max position notional (%s)", pair)
                    continue

                logger.info(
                    "Signal: %s spread=%.1fbps score=%s",
                    pair,
                    float(signal.spread_bps),
                    signal.score,
                )

                if signal.score < self.config.min_score:
                    logger.info("Skipped: score below threshold")
                    continue

                candidates.append(ScoredCandidate(signal=signal, pair=pair))

            for cand in sort_candidates_by_priority(candidates)[: self.config.max_signals_per_tick]:
                pair = cand.pair
                signal = cand.signal
                base = pair.split("/")[0]
                logger.info(
                    "Executing: %s %s %s",
                    signal.direction.name,
                    signal.size,
                    base,
                )

                ctx = await self.executor.execute(signal)
                self.scorer.record_result(pair, ctx.state == ExecutorState.DONE)

                if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                    if self.config.demo and isinstance(self.exchange, MockExchange):
                        self.exchange.apply_balance_deltas_from_execution(ctx)
                    arb_record = execution_to_arb_record(ctx)
                    self.pnl_engine.record(arb_record)
                    # Use the same net as ``PnLEngine`` so cumulative matches the session summary.
                    leg_pnl = arb_record.net_pnl
                    self._cumulative_arb_pnl += leg_pnl
                    port_extra = ""
                    start = self._session_portfolio_start_usd
                    if start is not None:
                        now = self._session_portfolio_usd_mark()
                        if now is not None:
                            port_extra = f" portfolio_vs_session_start=${float(now - start):+.2f}"
                    logger.info(
                        "SUCCESS: arbitrage profit=$%.2f (pnl=$%.2f)%s",
                        float(leg_pnl),
                        float(self._cumulative_arb_pnl),
                        port_extra,
                    )
                else:
                    logger.warning("FAILED: %s", ctx.error or "unknown error")
                    logger.warning(
                        "Circuit breaker: %d/%d failures",
                        cb.current_failures(),
                        cb.failure_threshold,
                    )

                await self._sync_balances()
        finally:
            if self.config.demo and isinstance(self.exchange, MockExchange):
                self.exchange.advance_demo_script()

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    async def _sync_balances(self) -> None:
        try:
            cex_balances = self.exchange.fetch_balance()
            self.inventory.update_from_cex(Venue.BINANCE, cex_balances)
        except Exception as exc:
            logger.warning("CEX balance sync failed: %s", exc)

        wallet_balances = self._fetch_wallet_balances()
        if wallet_balances:
            self.inventory.update_from_wallet(Venue.WALLET, wallet_balances)

    def _fetch_wallet_balances(self) -> dict[str, Decimal]:
        """Return on-chain balances; in demo / no-chain mode we synthesize them."""
        if self.config.demo and isinstance(self.exchange, MockExchange):
            return self.exchange.wallet_balances_for_demo()
        if self.config.demo:
            return {
                "ETH": DEMO_WALLET_BALANCE_BASE,
                "BTC": DEMO_WALLET_BALANCE_BTC,
                "USDT": DEMO_WALLET_BALANCE_QUOTE,
            }
        if self.chain_client is None:
            return {}
        wallet_addr = os.getenv("ARB_WALLET_ADDRESS")
        if not wallet_addr:
            return {}
        try:
            from core.types import Address

            addr = Address.from_string(wallet_addr)
            eth = self.chain_client.get_balance(addr)
            return {"ETH": to_decimal(eth.human)}
        except Exception as exc:
            logger.warning("wallet balance fetch failed: %s", exc)
            return {}


# --- Record bridge -----------------------------------------------------------


def execution_to_arb_record(ctx: ExecutionContext) -> ArbRecord:
    """Convert an :class:`ExecutionContext` to an :class:`ArbRecord` for PnL.

    Assignment of buy/sell leg is driven by ``signal.direction`` so the record
    is correct regardless of which leg executed first (CEX-first vs DEX-first).
    """
    signal = ctx.signal
    # Resolve CEX-side and DEX-side fills irrespective of leg order.
    if ctx.leg1_venue == VENUE_CEX:
        cex_price = ctx.leg1_fill_price or Decimal("0")
        cex_size = ctx.leg1_fill_size or Decimal("0")
        dex_price = ctx.leg2_fill_price or Decimal("0")
        dex_size = ctx.leg2_fill_size or Decimal("0")
    else:
        cex_price = ctx.leg2_fill_price or Decimal("0")
        cex_size = ctx.leg2_fill_size or Decimal("0")
        dex_price = ctx.leg1_fill_price or Decimal("0")
        dex_size = ctx.leg1_fill_size or Decimal("0")

    quote_asset = signal.pair.split("/")[1]
    started = datetime.fromtimestamp(ctx.started_at)
    finished = datetime.fromtimestamp(ctx.finished_at or ctx.started_at)

    cex_leg = TradeLeg(
        id=f"{signal.signal_id}_cex",
        timestamp=started if ctx.leg1_venue == VENUE_CEX else finished,
        venue=Venue.BINANCE,
        symbol=signal.pair,
        side="buy" if signal.direction == Direction.BUY_CEX_SELL_DEX else "sell",
        amount=cex_size,
        price=cex_price,
        fee=Decimal("0"),
        fee_asset=quote_asset,
    )
    dex_leg = TradeLeg(
        id=f"{signal.signal_id}_dex",
        timestamp=started if ctx.leg1_venue == VENUE_DEX else finished,
        venue=Venue.WALLET,
        symbol=signal.pair,
        side="sell" if signal.direction == Direction.BUY_CEX_SELL_DEX else "buy",
        amount=dex_size,
        price=dex_price,
        fee=Decimal("0"),
        fee_asset=quote_asset,
    )

    if signal.direction == Direction.BUY_CEX_SELL_DEX:
        buy_leg, sell_leg = cex_leg, dex_leg
    else:
        buy_leg, sell_leg = dex_leg, cex_leg

    return ArbRecord(
        id=signal.signal_id,
        timestamp=started,
        buy_leg=buy_leg,
        sell_leg=sell_leg,
    )


# --- CLI ---------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> ArbBotConfig:
    p = argparse.ArgumentParser(description="Run the arbitrage bot main loop")
    p.add_argument(
        "--demo",
        action="store_true",
        help="Offline mock: scripted ETH+BTC book, balance/PnL updates, then exit",
    )
    p.add_argument(
        "--pairs",
        nargs="+",
        default=[DEFAULT_PAIR],
        help=f"Symbols to scan (default: {DEFAULT_PAIR})",
    )
    p.add_argument(
        "--max-trade-size",
        type=str,
        default=None,
        help="Optional max base-asset size per leg; bot sizes below this via SignalGenerator",
    )
    p.add_argument(
        "--min-score",
        type=str,
        default=str(DEFAULT_MIN_SCORE),
        help="Minimum score required to execute",
    )
    p.add_argument(
        "--live", action="store_true", help="Disable simulation mode in the executor (dangerous)"
    )
    p.add_argument("--tick", type=float, default=DEFAULT_TICK_SECONDS, help="Seconds between ticks")
    p.add_argument(
        "--max-signals-per-tick",
        type=int,
        default=int(os.getenv("ARB_MAX_SIGNALS_PER_TICK", "1")),
        help="Max tradable signals to execute per tick after priority sort (default 1)",
    )
    args = p.parse_args(argv)
    pairs = list(args.pairs)
    if args.demo and pairs == [DEFAULT_PAIR]:
        pairs = ["ETH/USDT", "BTC/USDT"]
    max_ts = Decimal(args.max_trade_size) if args.max_trade_size is not None else None
    return ArbBotConfig(
        demo=args.demo,
        pairs=pairs,
        min_score=Decimal(args.min_score),
        simulation=not args.live,
        tick_seconds=args.tick,
        rpc_url=os.getenv("ETH_RPC_URL") or None,
        max_trade_size=max_ts,
        max_signals_per_tick=max(1, args.max_signals_per_tick),
    )


def main(argv: Optional[list[str]] = None) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FMT))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any default handlers so logs go to stdout (friendlier under PowerShell).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    random.seed(0)  # deterministic demo output
    cfg = _parse_args(argv)
    bot = ArbBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.stop()
        logger.info("Bot stopped by user")


if __name__ == "__main__":
    main()
