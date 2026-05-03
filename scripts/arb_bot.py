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
    Supports ``--dry-run`` / ``ARB_DRY_RUN`` (no trades; validation + risk only),
    kill-switch file (default ``/tmp/arb_bot_kill`` on Linux/macOS, process temp dir on
    Windows, or ``ARB_KILL_SWITCH_FILE``), optional Telegram
    alerts, optional Telegram slash commands (``TELEGRAM_CONTROLS_ENABLED=1``), and
    soft/hard risk limits (see ``risk/`` package).
    If ``ETH_RPC_URL`` or ``RPC_ENDPOINT`` is set, the bot constructs :class:`PricingEngine`, loads
    Uniswap V2 pools from ``ARB_V2_POOLS`` (comma-separated ``0x`` addresses) or
    built-in Arbitrum One defaults (WETH/USDT + WBTC/WETH), and wires
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
import time

from dotenv import load_dotenv
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from inventory.usd_mark import estimate_inventory_usd  # noqa: E402
from monitoring.daily_summary import generate_daily_summary  # noqa: E402
from monitoring.health_state import (
    BotHealth,
    TradeMetrics,
    build_trade_metrics,
    format_trade_metrics_log,
)  # noqa: E402
from monitoring.logging_setup import configure_arb_bot_logging  # noqa: E402
from monitoring.prometheus_metrics import PrometheusMetrics, try_start_metrics_server  # noqa: E402
from monitoring.telegram_alerts import TelegramNotifier, html_escape_text  # noqa: E402
from monitoring.telegram_control import (  # noqa: E402
    apply_slash_command,
    format_trade_failed_telegram,
    format_trade_success_telegram,
    poll_telegram_commands,
)
from monitoring.trade_csv_log import (  # noqa: E402
    TradeCsvJournal,
    build_trade_csv_row,
    production_flag_best_effort,
    trade_csv_path,
)
from risk.kill_switch import is_kill_switch_active  # noqa: E402
from risk.limits import RiskLimits  # noqa: E402
from risk.manager import RiskManager  # noqa: E402
from risk.pre_trade import PreTradeValidator  # noqa: E402
from risk.safety import ABSOLUTE_MIN_CAPITAL  # noqa: E402
from strategy.fees import DEFAULT_CEX_TAKER_BPS, FeeStructure  # noqa: E402
from strategy.generator import SignalGenerator  # noqa: E402
from strategy.scorer import SignalScorer  # noqa: E402
from strategy.signal import Direction, Signal, to_decimal  # noqa: E402
from strategy.signal_priority import ScoredCandidate, sort_candidates_by_priority  # noqa: E402

# --- Module constants --------------------------------------------------------
DEFAULT_MIN_SCORE = Decimal("60")
DEFAULT_TICK_SECONDS = 1.0
DEFAULT_ERROR_BACKOFF_SECONDS = 5.0
DEFAULT_PAIR = "ETH/USDT"

# Arbitrum One Uniswap V2 pairs (factory 0xf1D7...) when ARB_V2_POOLS is unset.
_DEFAULT_ARB_V2_POOLS: tuple[Address, ...] = (
    Address("0xd04Bc65744306A5C149414dd3CD5c984D9d3470d"),  # WETH/USDT
    Address("0x8c1D83A25eE2dA1643A5d937562682b1aC6C856B"),  # WBTC/WETH
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


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


_ENV_CEX_TAKER_BPS = "ARB_CEX_TAKER_BPS"


def _resolve_live_fee_structure(exchange: Any, pairs: list[str]) -> FeeStructure:
    """Build :class:`~strategy.fees.FeeStructure` for non-demo runs.

    Uses ``ARB_CEX_TAKER_BPS`` when set (basis points, no exchange call).
    Otherwise calls :meth:`exchange.client.ExchangeClient.max_taker_fee_bps_for_symbols`
    across ``pairs`` and falls back to :data:`~strategy.fees.DEFAULT_CEX_TAKER_BPS`.
    """
    manual = os.getenv(_ENV_CEX_TAKER_BPS, "").strip()
    if manual:
        try:
            d = to_decimal(manual)
            if d < 0:
                raise ValueError("negative")
            logger.info("CEX taker fee from %s=%s bps (manual)", _ENV_CEX_TAKER_BPS, d)
            return FeeStructure(cex_taker_bps=d)
        except Exception as exc:
            logger.warning(
                "invalid %s=%r (%s); will try exchange fetch",
                _ENV_CEX_TAKER_BPS,
                manual,
                exc,
            )
    try:
        max_bps = exchange.max_taker_fee_bps_for_symbols(list(pairs))
    except Exception as exc:
        logger.warning("CEX taker fee fetch failed: %s", exc)
        max_bps = None
    if max_bps is not None:
        logger.info("CEX taker fee from exchange: %s bps (max over %s)", max_bps, pairs)
        return FeeStructure(cex_taker_bps=max_bps)
    logger.info(
        "CEX taker fee: using default %s bps (set %s or fix exchange / credentials)",
        DEFAULT_CEX_TAKER_BPS,
        _ENV_CEX_TAKER_BPS,
    )
    return FeeStructure()


def _apply_signal_generator_env_overrides(cfg: dict[str, Any]) -> None:
    """Tune :class:`~strategy.generator.SignalGenerator` without code edits (live/dry-run)."""
    mapping: tuple[tuple[str, str, str], ...] = (
        ("ARB_MIN_SPREAD_BPS", "min_spread_bps", "decimal"),
        ("ARB_MIN_PROFIT_USD", "min_profit_usd", "decimal"),
        ("ARB_MAX_POSITION_USD", "max_position_usd", "decimal"),
        ("ARB_SIGNAL_TTL_SECONDS", "signal_ttl_seconds", "float"),
        ("ARB_SIGNAL_COOLDOWN_SECONDS", "cooldown_seconds", "float"),
    )
    for env_name, key, kind in mapping:
        raw = os.getenv(env_name, "").strip()
        if not raw:
            continue
        try:
            if kind == "decimal":
                cfg[key] = to_decimal(raw)
            else:
                cfg[key] = float(raw)
        except Exception as exc:
            logger.warning("ignored invalid %s=%r (%s)", env_name, raw, exc)


_ENV_TELEGRAM_NOTIFY_OPPORTUNITIES = "ARB_TELEGRAM_NOTIFY_OPPORTUNITIES"
_ENV_TELEGRAM_OPPORTUNITY_COOLDOWN_SEC = "ARB_TELEGRAM_OPPORTUNITY_COOLDOWN_SEC"
_DEFAULT_OPPORTUNITY_COOLDOWN_SEC = 300.0
_ENV_KILL_SWITCH_POLL_SEC = "ARB_KILL_SWITCH_POLL_SEC"
_DEFAULT_KILL_POLL_SEC = 3.0
_ENV_AUTO_CAPITAL_EMERGENCY_STOP = "ARB_AUTO_CAPITAL_EMERGENCY_STOP"

# Dry-run pipeline mode. ``log`` (default, legacy) just records DRY_RUN lines.
# ``signed`` runs the full production flow on the DEX leg (route + fork
# preflight + EIP-1559 build + signing) but skips broadcasting; the CEX leg
# stays in simulation so no real Binance order is placed. The structured
# per-tick console line is rendered in either mode.
_ENV_DRY_RUN_MODE = "ARB_DRY_RUN_MODE"
_DRY_RUN_MODE_LOG = "log"
_DRY_RUN_MODE_SIGNED = "signed"

# Optional override for wallet inventory in dry-run, e.g. ``ETH=2,USDC=5000``.
# Honored only when the bot is in dry-run mode so live runs cannot accidentally
# operate on synthetic balances.
_ENV_VIRTUAL_BALANCES = "ARB_VIRTUAL_BALANCES"
# Same idea, but for the CEX side (Binance free-balance dictionary). Useful when
# the live account does not hold the quote/base asset under test.
_ENV_VIRTUAL_CEX_BALANCES = "ARB_VIRTUAL_CEX_BALANCES"

# How often (seconds) to re-fetch loaded V2 pool reserves so the math-only DEX
# quote in :class:`SignalGenerator` tracks live LP activity instead of being
# frozen at the value loaded at startup. Set to ``0`` to disable.
_ENV_POOL_REFRESH_SECONDS = "ARB_POOL_REFRESH_SECONDS"
_DEFAULT_POOL_REFRESH_SECONDS = 5.0

# Symbol aliases the inventory tracker uses when it queries a CEX-style ticker
# against an on-chain ERC20 reading (Binance trades ETH/BTC, on-chain liquidity
# uses WETH/WBTC). Keep keys uppercase.
_WALLET_SYMBOL_ALIASES: dict[str, str] = {
    "WETH": "ETH",
    "WBTC": "BTC",
}


def _opportunity_telegram_cooldown_sec() -> float:
    raw = os.getenv(_ENV_TELEGRAM_OPPORTUNITY_COOLDOWN_SEC, "").strip()
    if not raw:
        return _DEFAULT_OPPORTUNITY_COOLDOWN_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_OPPORTUNITY_COOLDOWN_SEC


def _format_telegram_startup(config: ArbBotConfig) -> str:
    """HTML body for Telegram on bot start (UTC time, mode flags, pairs)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    flags: list[str] = []
    if config.dry_run:
        flags.append("dry_run")
    if config.demo:
        flags.append("demo")
    if config.simulation:
        flags.append("simulation")
    exec_label = "simulation (no live DEX legs)" if config.simulation else "live execution path"
    pairs = ",".join(config.pairs)
    tick_s = f"{config.tick_seconds:g}"
    return "\n".join(
        [
            "<b>Arb bot started</b>",
            f"time: <code>{html_escape_text(ts)}</code>",
            f"flags: <code>{html_escape_text(','.join(flags) or 'none')}</code>",
            f"executor: <code>{html_escape_text(exec_label)}</code>",
            f"tick_s: <code>{html_escape_text(tick_s)}</code>",
            f"pairs: <code>{html_escape_text(pairs)}</code>",
        ],
    )


def _format_telegram_stopped_line() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"Bot stopped at <code>{html_escape_text(ts)}</code>"


_ENV_BALANCE_VERIFY_DISABLED = "ARB_BALANCE_VERIFY_DISABLED"
_ENV_BALANCE_TOLERANCE = "ARB_BALANCE_VERIFY_TOLERANCE"
_DEFAULT_BALANCE_TOLERANCE = Decimal("0.001")


def _balance_verify_enabled(*, demo: bool) -> bool:
    if demo:
        return False
    if _env_truthy(_ENV_BALANCE_VERIFY_DISABLED):
        return False
    return True


def _balance_tolerance() -> Decimal:
    raw = os.getenv(_ENV_BALANCE_TOLERANCE, "").strip()
    return to_decimal(raw) if raw else _DEFAULT_BALANCE_TOLERANCE


def _cex_free_decimal(balances: dict[str, Any], asset: str) -> Decimal:
    """Normalize CCXT-style ``{asset: {free: ...}}`` to Decimal."""
    row = balances.get(asset)
    if not isinstance(row, dict):
        row = balances.get(asset.upper())
    if not isinstance(row, dict):
        return Decimal("0")
    return to_decimal(row.get("free", 0))


def _parse_virtual_balances(raw: str) -> dict[str, Decimal]:
    """Parse ``ARB_VIRTUAL_BALANCES`` into ``{SYMBOL: Decimal}`` (silently skips invalid pairs).

    Wallet aliasing is applied (``WETH`` -> ``ETH``, ``WBTC`` -> ``BTC``) so the
    inventory keys line up with the CEX-style tickers the rest of the bot uses.
    """
    out: dict[str, Decimal] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        sym = key.strip().upper()
        if not sym:
            continue
        try:
            amt = to_decimal(value.strip())
        except Exception:
            continue
        if amt < 0:
            continue
        out[_WALLET_SYMBOL_ALIASES.get(sym, sym)] = amt
    return out


def _apply_cex_virtual_overrides(
    real: Any,
    overrides: dict[str, Decimal],
) -> dict[str, dict[str, Decimal]]:
    """Merge dry-run CEX overrides into the CCXT-style balance dict.

    Each override asset becomes (or replaces) a fully-funded ``free``/
    ``locked``/``total`` row so :meth:`InventoryTracker.update_from_cex`
    treats it the same way it treats a real Binance row. Other rows in
    ``real`` are preserved untouched.
    """
    if isinstance(real, dict):
        out: dict[str, dict[str, Decimal]] = {}
        for k, v in real.items():
            if isinstance(v, dict):
                out[k] = dict(v)
            else:
                out[k] = v  # keep CCXT meta keys (info, free, used, total maps)
    else:
        out = {}
    for asset, amt in overrides.items():
        out[asset] = {
            "free": amt,
            "locked": Decimal("0"),
            "used": Decimal("0"),
            "total": amt,
        }
    return out


def _format_dec(value: Optional[Decimal], places: int) -> str:
    """Render a Decimal with fixed places, or ``N/A`` for ``None``."""
    if value is None:
        return "N/A"
    try:
        d = to_decimal(value)
    except Exception:
        return "N/A"
    quant = Decimal(1).scaleb(-places) if places > 0 else Decimal(1)
    try:
        return f"{d.quantize(quant):f}"
    except Exception:
        return f"{float(d):.{places}f}"


def format_dryrun_console_line(
    pair: str,
    snapshot: Optional[dict[str, Any]],
    signal: Optional[Signal],
    sent: str,
    *,
    base_symbol: Optional[str] = None,
) -> str:
    """One-line per-tick summary used by both dry-run modes (and live)."""
    base = base_symbol or pair.split("/")[0]
    if snapshot is None:
        return (
            f"{pair} | bid N/A | ask N/A | dex N/A | spread N/A bps | "
            f"est_profit $0.00 | sent={sent}"
        )
    bid = snapshot.get("cex_bid")
    ask = snapshot.get("cex_ask")
    bid_size = snapshot.get("cex_bid_size")
    ask_size = snapshot.get("cex_ask_size")
    if signal is not None and signal.direction == Direction.BUY_DEX_SELL_CEX:
        dex_price = snapshot.get("dex_buy")
    elif signal is not None and signal.direction == Direction.BUY_CEX_SELL_DEX:
        dex_price = snapshot.get("dex_sell")
    else:
        # No signal yet: report the better of the two DEX quotes (whichever is
        # closer to crossing the CEX book) so the operator still sees a number.
        buy = snapshot.get("dex_buy")
        sell = snapshot.get("dex_sell")
        dex_price = sell if (buy is None or (sell is not None and sell > buy)) else buy

    spread_bps: Optional[Decimal]
    if signal is not None:
        # ``Signal.spread_bps`` is the directional edge the generator picked.
        spread_bps = signal.spread_bps
    else:
        # No signal: render the best-edge bps observed against CEX mid as a
        # quick "how close are we to a tradable spread?" indicator.
        cex_mid = snapshot.get("cex_mid") or Decimal("0")
        if dex_price is not None and cex_mid > 0:
            spread_bps = (
                (to_decimal(dex_price) - to_decimal(cex_mid))
                / to_decimal(cex_mid)
                * Decimal("10000")
            )
        else:
            spread_bps = None
    est_profit = signal.expected_net_pnl if signal is not None else Decimal("0")

    dex_src = snapshot.get("dex_source") or "?"
    return (
        f"{pair} | "
        f"bid {_format_dec(bid, 2)} x {_format_dec(bid_size, 4)} {base} | "
        f"ask {_format_dec(ask, 2)} x {_format_dec(ask_size, 4)} {base} | "
        f"dex {_format_dec(dex_price, 2)} ({dex_src}) | "
        f"spread {_format_dec(spread_bps, 2)} bps | "
        f"est_profit ${_format_dec(est_profit, 2)} | "
        f"sent={sent}"
    )


def format_dryrun_signed_telegram(
    *,
    pair: str,
    leg_pnl: Decimal,
    cumulative: Decimal,
    metrics_line: str,
    signed_tx_hash: Optional[str],
    raw_tx_hex_preview: str,
    preflight_gas_used: Optional[int],
) -> str:
    """HTML body for Telegram on a signed-but-not-broadcast dry-run trade."""
    gas_str = "n/a" if preflight_gas_used is None else str(preflight_gas_used)
    short_hash = (signed_tx_hash or "n/a")[:18]
    return (
        "<b>[DRY-RUN] Trade SIGNED (not broadcast)</b>\n"
        f"pair: <code>{html_escape_text(pair)}</code>\n"
        f"net_pnl: <code>{html_escape_text(str(leg_pnl))}</code>\n"
        f"session_cumulative: <code>{html_escape_text(str(cumulative))}</code>\n"
        f"signed_tx_hash: <code>{html_escape_text(short_hash)}</code>\n"
        f"preflight_gas_used: <code>{html_escape_text(gas_str)}</code>\n"
        f"raw_tx_preview: <code>{html_escape_text(raw_tx_hex_preview)}</code>\n"
        f"<code>{html_escape_text(metrics_line)}</code>"
    )


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
    dry_run: bool = False
    # ``log`` keeps the legacy "log + continue" dry-run branch (default; required
    # for tests that assert ``execute`` is never called). ``signed`` opts into
    # the new pipeline that builds + signs (but never broadcasts) the DEX tx
    # while leaving the CEX leg in simulation.
    dry_run_mode: str = _DRY_RUN_MODE_LOG

    def __post_init__(self) -> None:
        self.min_score = to_decimal(self.min_score)
        if self.max_trade_size is not None:
            self.max_trade_size = to_decimal(self.max_trade_size)
            if self.max_trade_size <= 0:
                raise ValueError("max_trade_size must be positive when set")
        if self.max_signals_per_tick < 1:
            raise ValueError("max_signals_per_tick must be >= 1")
        mode = (self.dry_run_mode or _DRY_RUN_MODE_LOG).strip().lower()
        if mode not in (_DRY_RUN_MODE_LOG, _DRY_RUN_MODE_SIGNED):
            raise ValueError(
                f"dry_run_mode must be {_DRY_RUN_MODE_LOG!r} or {_DRY_RUN_MODE_SIGNED!r}, "
                f"got {self.dry_run_mode!r}",
            )
        self.dry_run_mode = mode

    @property
    def dry_run_signed(self) -> bool:
        """True when dry-run should build + sign the DEX tx (but skip broadcast)."""
        return self.dry_run and self.dry_run_mode == _DRY_RUN_MODE_SIGNED


class ArbBot:
    """Run loop that glues SignalGenerator, SignalScorer, and Executor."""

    def __init__(self, config: ArbBotConfig) -> None:
        self.config = config
        self.running = False
        self._telegram = TelegramNotifier()
        self._trading_paused = False
        self._telegram_update_offset = 0
        self._telegram_controls_enabled = _env_truthy("TELEGRAM_CONTROLS_ENABLED")
        self._halt_on_consecutive_losses = _env_truthy("ARB_HALT_ON_CONSECUTIVE_LOSSES")
        self._last_pause_log_mono = 0.0
        self._last_opportunity_telegram_mono: float = 0.0
        self._trade_journal = TradeCsvJournal(trade_csv_path())
        self._production_binance = production_flag_best_effort()
        self._last_heartbeat_log_mono: float = 0.0
        # Monotonic timestamp of the last on-chain pool reserve refresh.
        # Driven by ``ARB_POOL_REFRESH_SECONDS`` so the math-only DEX quote
        # in ``SignalGenerator`` tracks live LP activity.
        self._last_pool_refresh_mono: float = 0.0
        self.health = BotHealth()
        self.pre_trade_validator = PreTradeValidator()
        initial_capital = to_decimal(os.getenv("ARB_INITIAL_CAPITAL", "100"))
        limits = RiskLimits.defaults() if config.demo else RiskLimits.from_env()
        self.risk_manager = RiskManager(limits, initial_capital)

        self._metrics = PrometheusMetrics()

        metrics_port = int(os.getenv("PROMETHEUS_METRICS_PORT", "0") or "0")
        if metrics_port > 0:
            srv = try_start_metrics_server(metrics_port)
            if srv is not None:
                logger.info("Prometheus /metrics listening on port %s", metrics_port)

        def _trip_metric(_cb: Any) -> None:
            self._metrics.record_circuit_trip()

        def _telegram_circuit_trip(_cb: Any) -> None:
            if self._telegram.enabled:
                self._telegram.send("Circuit breaker tripped", urgent=True)

        wh_url = (
            os.getenv("ARB_CIRCUIT_WEBHOOK_URL") or os.getenv("ARB_WEBHOOK_URL") or ""
        ).strip()
        wh_timeout = float(os.getenv("ARB_WEBHOOK_TIMEOUT_SECONDS", "5") or "5")
        trip_parts = [_trip_metric, _telegram_circuit_trip]
        if wh_url:
            trip_parts.append(
                make_circuit_breaker_webhook_hook(
                    WebhookDeliveryConfig(url=wh_url, timeout_seconds=wh_timeout),
                ),
            )
        self._circuit_on_trip = chain_trip_hooks(*trip_parts)

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
        # Demo: zero gas so scripted sizes stay profitable. Non-demo: CEX taker bps
        # from exchange (or ARB_CEX_TAKER_BPS / default); DEX + gas from FeeStructure defaults.
        self.fees = (
            FeeStructure(gas_cost_usd=Decimal("0"))
            if config.demo
            else _resolve_live_fee_structure(self.exchange, config.pairs)
        )

        signal_cfg = dict(DEMO_SIGNAL_CONFIG if config.demo else {})
        signal_cfg.update(config.signal_config or {})
        if not config.demo:
            _apply_signal_generator_env_overrides(signal_cfg)
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

        logger.info(
            "Signal thresholds: min_spread_bps=%s min_profit_usd=%s max_position_usd=%s "
            "cooldown_s=%s min_score=%s dry_run=%s cex_taker_bps=%s",
            self.generator.min_spread_bps,
            self.generator.min_profit_usd,
            self.generator.max_position_usd,
            self.generator.cooldown,
            config.min_score,
            config.dry_run,
            self.fees.cex_taker_bps,
        )

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
            dex_dry_run_signed=False,  # set after wallet load below if applicable
        )

        dex_wallet = None
        wants_live_dex = (
            dex_live
            and not config.simulation
            and not config.demo
            and self.pricing_engine is not None
            and self._token_resolver is not None
        )
        # Signed dry-run also needs a real wallet (to sign the not-broadcast tx).
        # CEX leg stays in simulation, so this can run with ``--dry-run`` (no
        # ``--live``) provided the chain stack + pricing engine + token resolver
        # are wired and the kill-switch / risk gates allow it.
        wants_dry_run_signed = (
            config.dry_run_signed
            and not config.demo
            and self.pricing_engine is not None
            and self._token_resolver is not None
        )
        if wants_live_dex or wants_dry_run_signed:
            try:
                from core.wallet import WalletManager

                dex_wallet = WalletManager.from_env("PRIVATE_KEY")
                if wants_live_dex:
                    logger.info("DEX_LIVE_ENABLED: live router swaps enabled for this process")
                if wants_dry_run_signed:
                    logger.info(
                        "ARB_DRY_RUN_MODE=signed: DEX leg will build + fork-preflight + sign "
                        "(NO broadcast); CEX leg stays simulated",
                    )
                    executor_config.dex_dry_run_signed = True
            except Exception as exc:
                logger.warning(
                    "wallet init failed (%s); DEX leg will fail closed (dry-run-signed disabled)",
                    exc,
                )
                executor_config.dex_dry_run_signed = False

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

        _cb = self.executor.circuit_breaker
        logger.info(
            "Circuit breaker: failure_threshold=%d window_s=%.0f cooldown_s=%.0f",
            _cb.failure_threshold,
            _cb.config.window_seconds,
            _cb.config.cooldown_seconds,
        )
        if self._trade_journal.enabled and self._trade_journal.path is not None:
            logger.info(
                "Trade CSV journal: %s (override with ARB_TRADE_CSV=path)",
                self._trade_journal.path,
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
        self.health.is_running = True
        self._demo_cb_open_logged = False
        self._cumulative_arb_pnl = Decimal("0")
        logger.info("Bot starting...")
        control_task: asyncio.Task[Any] | None = None
        if self._telegram_controls_enabled and self._telegram.enabled:
            control_task = asyncio.create_task(self._telegram_control_loop())
            logger.info(
                "Telegram command control enabled (TELEGRAM_CONTROLS_ENABLED=1); poll every %s s",
                os.getenv("TELEGRAM_CONTROL_POLL_SEC", "3"),
            )
        try:
            if not self.config.demo:
                try:
                    from config.config import PRODUCTION as _production

                    if _production:
                        logger.warning(
                            "PRODUCTION MODE — Binance production keys / endpoints; "
                            "verify pre-flight checklist.",
                        )
                    else:
                        logger.info("Binance testnet mode (PRODUCTION=false in environment)")
                except Exception as exc:
                    logger.debug("Could not read production flag: %s", exc)
            if self._telegram.enabled:
                self._telegram.send(_format_telegram_startup(self.config))
            await self._sync_balances()
            self._session_portfolio_start_usd = self._session_portfolio_usd_mark()
            self._last_heartbeat_log_mono = time.monotonic()

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
                    await self._sleep_respecting_kill_switch(self.config.tick_seconds)
                    now_m = time.monotonic()
                    if now_m - self._last_heartbeat_log_mono >= 60.0:
                        self._last_heartbeat_log_mono = now_m
                        now_wall = time.time()
                        hb_age = (
                            (now_wall - self.health.last_heartbeat)
                            if self.health.last_heartbeat > 0
                            else -1.0
                        )
                        logger.info(
                            "HEARTBEAT|capital_usd=%s|daily_pnl=%s|cb_open=%s|running=%s|"
                            "last_tick_hb_age_s=%.1f",
                            self.health.current_capital,
                            self.health.daily_pnl,
                            self.health.circuit_breaker_open,
                            self.running,
                            hb_age,
                        )
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("Tick error: %s", exc)
                    self.health.record_error()
                    await asyncio.sleep(self.config.error_backoff_seconds)
        finally:
            if control_task is not None:
                control_task.cancel()
                try:
                    await control_task
                except asyncio.CancelledError:
                    pass
            self.health.is_running = False

    async def _telegram_control_loop(self) -> None:
        poll_sec = float(os.getenv("TELEGRAM_CONTROL_POLL_SEC", "3") or "3")
        poll_sec = max(1.0, poll_sec)
        while self.running:
            try:
                self._telegram_update_offset, batch = await asyncio.to_thread(
                    poll_telegram_commands,
                    self._telegram,
                    self._telegram_update_offset,
                    timeout=0,
                )
                for text, _uid in batch:
                    reply = apply_slash_command(self, text)
                    if reply is not None:
                        self._telegram.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("telegram control loop tick")
            await asyncio.sleep(poll_sec)

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

    def stop(self) -> None:
        self.running = False
        self.health.is_running = False

    def on_shutdown(self) -> None:
        """Best-effort Telegram summary when the process stops."""
        if self._telegram.enabled:
            summary = generate_daily_summary(
                self.pnl_engine,
                current_capital=estimate_inventory_usd(self.inventory),
            )
            self._telegram.send(html_escape_text(summary))
            self._telegram.send(_format_telegram_stopped_line())

    def _shutdown_from_kill_switch(self) -> None:
        if not self.running:
            return
        logger.critical("KILL SWITCH ACTIVE — stopping bot")
        if self._telegram.enabled:
            self._telegram.send("Kill switch active; bot stopping.", urgent=True)
        self.stop()

    def _shutdown_from_capital_emergency(self, cap: Decimal) -> None:
        if not self.running:
            return
        logger.critical(
            "CAPITAL EMERGENCY STOP — inventory USD estimate %s below ABSOLUTE_MIN_CAPITAL %s",
            cap,
            ABSOLUTE_MIN_CAPITAL,
        )
        if self._telegram.enabled:
            self._telegram.send(
                html_escape_text(
                    f"Capital emergency stop: estimate {cap} USD < {ABSOLUTE_MIN_CAPITAL} USD",
                ),
                urgent=True,
            )
        self.stop()

    def _kill_switch_poll_chunk_sec(self) -> float:
        raw = os.getenv(_ENV_KILL_SWITCH_POLL_SEC, "").strip()
        if not raw:
            return _DEFAULT_KILL_POLL_SEC
        try:
            return max(0.5, float(raw))
        except ValueError:
            return _DEFAULT_KILL_POLL_SEC

    def _maybe_refresh_pool_reserves(self) -> None:
        """Re-fetch loaded V2 pool reserves on a fixed cadence.

        The math-only DEX quote in :class:`SignalGenerator` reads reserves
        directly out of in-memory :class:`UniswapV2Pair` objects. Without a
        periodic refresh those reserves are frozen at the value sampled when
        :meth:`PricingEngine.load_pools` ran at startup, so the displayed DEX
        price never moves and arbitrage opportunities go silently stale.

        Cadence is controlled by ``ARB_POOL_REFRESH_SECONDS`` (default
        :data:`_DEFAULT_POOL_REFRESH_SECONDS`); set ``0`` to disable. Each
        refresh issues one ``getReserves`` ``eth_call`` per loaded pool, which
        is cheap relative to typical RPC budgets (one pool ≈ one call every
        few seconds).
        """
        if self.pricing_engine is None:
            return
        if not self.pricing_engine.pools:
            return

        raw = os.getenv(_ENV_POOL_REFRESH_SECONDS, "").strip()
        try:
            interval = float(raw) if raw else _DEFAULT_POOL_REFRESH_SECONDS
        except ValueError:
            interval = _DEFAULT_POOL_REFRESH_SECONDS
        if interval <= 0:
            return

        now = time.monotonic()
        if now - self._last_pool_refresh_mono < interval:
            return
        self._last_pool_refresh_mono = now

        for addr in list(self.pricing_engine.pools.keys()):
            try:
                self.pricing_engine.refresh_pool(addr)
            except Exception as exc:
                logger.warning(
                    "Pool reserve refresh failed for %s: %s",
                    addr.checksum,
                    exc,
                )

    async def _sleep_respecting_kill_switch(self, seconds: float) -> None:
        """Sleep between ticks in short chunks so a kill file is noticed quickly."""
        remaining = float(seconds)
        if remaining <= 0:
            return
        chunk_cap = self._kill_switch_poll_chunk_sec()
        while self.running and remaining > 0:
            step = min(chunk_cap, remaining)
            await asyncio.sleep(step)
            remaining -= step
            if is_kill_switch_active():
                self._shutdown_from_kill_switch()
                return

    async def _tick(self) -> None:
        if is_kill_switch_active():
            self._shutdown_from_kill_switch()
            return

        if not self.config.demo and _env_truthy(_ENV_AUTO_CAPITAL_EMERGENCY_STOP):
            cap = estimate_inventory_usd(self.inventory)
            if cap < ABSOLUTE_MIN_CAPITAL:
                self._shutdown_from_capital_emergency(cap)
                return

        cb = self.executor.circuit_breaker
        if cb.is_open():
            if not self._demo_cb_open_logged:
                self._demo_cb_open_logged = True
                logger.warning(
                    "Circuit breaker open (%.0fs until reset)",
                    cb.time_until_reset(),
                )
            self.health.circuit_breaker_open = True
            self.health.touch_heartbeat()
            return

        self.health.circuit_breaker_open = False

        if self._trading_paused:
            self.health.touch_heartbeat()
            now_m = time.monotonic()
            if now_m - self._last_pause_log_mono >= 120.0:
                self._last_pause_log_mono = now_m
                logger.info("Trading paused (Telegram /resume to continue)")
            return

        # Per-tick status used by the structured console line. Keys are pairs;
        # values are (signal_or_None, sent_string, reason_or_None).
        pair_status: dict[str, tuple[Optional[Signal], str]] = {}

        # Keep the math-only DEX quote in :class:`SignalGenerator` honest by
        # periodically re-reading pool reserves from the chain. Without this
        # the displayed DEX price freezes at the value loaded at startup and
        # the spread looks suspiciously stable.
        self._maybe_refresh_pool_reserves()

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
                            logger.debug(
                                "No opportunity: no tradeable edge (demo scripted %.1f bps)",
                                float(s),
                            )
                            no_opp_logged = True
                    # Surface the underlying reason from the generator
                    # (no_edge, inventory_blocked, below_min_profit, ...) so the
                    # operator can fix the right thing instead of guessing.
                    reason = getattr(self.generator, "last_reason", {}).get(pair, "no_opportunity")
                    pair_status[pair] = (None, f"NO reason={reason}")
                    continue

                ok_pre, reason_pre = self.pre_trade_validator.validate_signal(signal)
                if not ok_pre:
                    logger.warning("Pre-trade validation failed: %s (%s)", reason_pre, pair)
                    pair_status[pair] = (signal, f"NO reason=pre_trade:{reason_pre}")
                    continue

                if not self.config.demo:
                    cap = estimate_inventory_usd(self.inventory)
                    ok_risk, reason_risk = self.risk_manager.check_pre_trade(
                        signal, total_capital=cap
                    )
                    if not ok_risk:
                        logger.warning("Risk check failed: %s (%s)", reason_risk, pair)
                        if reason_risk.startswith("safety:") and self._telegram.enabled:
                            self._telegram.send(
                                html_escape_text(f"Risk safety gate: {reason_risk}"),
                                urgent=True,
                            )
                        pair_status[pair] = (signal, f"NO reason=risk:{reason_risk}")
                        continue

                signal.score = self.scorer.score(signal, self.inventory.get_skews())

                logger.debug(
                    "Signal: %s spread=%s bps score=%s",
                    pair,
                    str(signal.spread_bps),
                    str(signal.score),
                )

                if signal.score < self.config.min_score:
                    logger.debug("Skipped: score below threshold (pair=%s)", pair)
                    pair_status[pair] = (signal, "NO reason=score_below_min")
                    continue

                # Tentative status until execution decides; gets overwritten below.
                pair_status[pair] = (signal, "NO reason=queued")
                candidates.append(ScoredCandidate(signal=signal, pair=pair))

            if (
                candidates
                and _env_truthy(_ENV_TELEGRAM_NOTIFY_OPPORTUNITIES)
                and self._telegram.enabled
            ):
                now_m = time.monotonic()
                if (
                    now_m - self._last_opportunity_telegram_mono
                    >= _opportunity_telegram_cooldown_sec()
                ):
                    self._last_opportunity_telegram_mono = now_m
                    ordered = sort_candidates_by_priority(candidates)
                    lines = [
                        "<b>Opportunities (this tick)</b>",
                        f"count: <code>{len(ordered)}</code>",
                    ]
                    for i, cand in enumerate(ordered[:5]):
                        s = cand.signal
                        lines.append(
                            f"{i + 1}. <code>{html_escape_text(cand.pair)}</code> "
                            f"spread_bps=<code>{html_escape_text(str(s.spread_bps))}</code> "
                            f"score=<code>{html_escape_text(str(s.score))}</code> "
                            f"net_usd=<code>{html_escape_text(str(s.expected_net_pnl))}</code> "
                            f"dir=<code>{html_escape_text(s.direction.value)}</code>",
                        )
                    self._telegram.send("\n".join(lines))

            for cand in sort_candidates_by_priority(candidates)[: self.config.max_signals_per_tick]:
                pair = cand.pair
                signal = cand.signal
                base = pair.split("/")[0]
                logger.debug(
                    "Executing: %s %s %s",
                    signal.direction.name,
                    signal.size,
                    base,
                )

                # ---- Dry-run, mode=log: legacy "log + continue". Required by
                # tests/test_arb_bot_dry_run.py which asserts execute() is never
                # awaited in this path.
                if self.config.dry_run and not self.config.dry_run_signed:
                    logger.info(
                        "DRY_RUN|pair=%s|direction=%s|size=%s|spread_bps=%s|expected_net_pnl=%s",
                        pair,
                        signal.direction.value,
                        str(signal.size),
                        str(signal.spread_bps),
                        str(signal.expected_net_pnl),
                    )
                    if self._trade_journal.enabled:
                        self._trade_journal.append_row(
                            build_trade_csv_row(
                                outcome="dry_run",
                                pair=pair,
                                signal=signal,
                                event_mono=time.monotonic(),
                                config_demo=self.config.demo,
                                config_dry_run=True,
                                config_simulation=self.config.simulation,
                                production_binance=self._production_binance,
                                min_score=self.config.min_score,
                                tick_seconds=self.config.tick_seconds,
                            ),
                        )
                    pair_status[pair] = (
                        signal,
                        f"NO (DRY-RUN log_only) est_profit=${signal.expected_net_pnl}",
                    )
                    continue

                self.risk_manager.open_positions = 1
                try:
                    ctx = await self.executor.execute(signal)
                finally:
                    self.risk_manager.open_positions = 0

                self.scorer.record_result(pair, ctx.state == ExecutorState.DONE)

                # Detect "signed but not broadcast" — the dry-run-signed path
                # marks the leg result with ``dry_run=True`` and prefixes the
                # synthetic tx hash with ``0xDRYRUN``.
                signed_dry_run = bool(
                    ctx.metadata.get("leg2_dry_run") or ctx.metadata.get("leg1_dry_run")
                )

                arb_record: ArbRecord | None = None
                tm: TradeMetrics | None = None
                balance_verify = "skipped"

                if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                    if not signed_dry_run:
                        self.risk_manager.record_trade(ctx.actual_net_pnl)
                        if (
                            self._halt_on_consecutive_losses
                            and ctx.actual_net_pnl < 0
                            and self.risk_manager.consecutive_losses
                            >= self.risk_manager.limits.consecutive_loss_limit
                        ):
                            msg_halt = (
                                f"Halt: consecutive loss limit reached "
                                f"({self.risk_manager.consecutive_losses})"
                            )
                            logger.warning(msg_halt)
                            if self._telegram.enabled:
                                self._telegram.send(html_escape_text(msg_halt), urgent=True)
                            self.stop()
                    if self.config.demo and isinstance(self.exchange, MockExchange):
                        self.exchange.apply_balance_deltas_from_execution(ctx)
                    arb_record = execution_to_arb_record(ctx)
                    self.pnl_engine.record(arb_record)
                    leg_pnl = arb_record.net_pnl
                    self._cumulative_arb_pnl += leg_pnl
                    self.health.last_trade_time = time.time()
                    tm = build_trade_metrics(signal, ctx, arb_record)
                    metrics_line = format_trade_metrics_log(tm)
                    if signed_dry_run:
                        signed_hash = ctx.metadata.get("leg2_signed_tx_hash") or ctx.metadata.get(
                            "leg1_signed_tx_hash"
                        )
                        raw_hex = (
                            ctx.metadata.get("leg2_signed_raw_tx_hex")
                            or ctx.metadata.get("leg1_signed_raw_tx_hex")
                            or ""
                        )
                        gas_used = ctx.metadata.get("leg2_preflight_gas_used") or ctx.metadata.get(
                            "leg1_preflight_gas_used"
                        )
                        raw_preview = raw_hex[:42] + ("..." if len(raw_hex) > 42 else "")
                        logger.info(
                            "[DRY-RUN] TRADE|%s|signed_tx=%s|gas=%s|%s",
                            pair,
                            (signed_hash or "n/a")[:18],
                            gas_used if gas_used is not None else "n/a",
                            metrics_line,
                        )
                        if self._telegram.enabled:
                            self._telegram.send(
                                format_dryrun_signed_telegram(
                                    pair=pair,
                                    leg_pnl=leg_pnl,
                                    cumulative=self._cumulative_arb_pnl,
                                    metrics_line=metrics_line,
                                    signed_tx_hash=signed_hash,
                                    raw_tx_hex_preview=raw_preview,
                                    preflight_gas_used=gas_used,
                                ),
                            )
                        pair_status[pair] = (
                            signal,
                            f"NO (DRY-RUN signed_tx={(signed_hash or 'n/a')[:18]})",
                        )
                    else:
                        logger.info("TRADE|%s|%s", pair, metrics_line)
                        if self._telegram.enabled:
                            self._telegram.send(
                                format_trade_success_telegram(
                                    pair=pair,
                                    leg_pnl=leg_pnl,
                                    cumulative=self._cumulative_arb_pnl,
                                    metrics_line=metrics_line,
                                ),
                            )
                        pair_status[pair] = (
                            signal,
                            f"YES tx={(ctx.leg2_tx_hash or 'n/a')[:18]}",
                        )
                    port_extra = ""
                    start = self._session_portfolio_start_usd
                    if start is not None:
                        now = self._session_portfolio_usd_mark()
                        if now is not None:
                            port_extra = f" portfolio_vs_session_start=${float(now - start):+.2f}"
                    success_label = "DRY-RUN SIGNED" if signed_dry_run else "SUCCESS"
                    logger.info(
                        "%s: arbitrage profit=$%.2f (pnl=$%.2f)%s",
                        success_label,
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
                    tm = build_trade_metrics(signal, ctx, None)
                    if self._telegram.enabled:
                        self._telegram.send(
                            format_trade_failed_telegram(
                                pair=pair,
                                error=ctx.error or "unknown",
                                direction=signal.direction.value,
                                size=str(signal.size),
                                spread_bps=str(signal.spread_bps),
                            ),
                        )
                    pair_status[pair] = (signal, f"NO error={(ctx.error or 'unknown')[:48]}")

                # Skip post-trade balance refetch for signed-dry-run: nothing
                # actually moved on chain or on the exchange.
                if not signed_dry_run:
                    await self._sync_balances()
                    if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                        balance_verify = await self.verify_balances_post_trade(pair)

                if self._trade_journal.enabled:
                    if signed_dry_run:
                        outcome = (
                            "dry_run_signed"
                            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None
                            else "dry_run_signed_failed"
                        )
                    else:
                        outcome = (
                            "executed_done"
                            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None
                            else "executed_failed"
                        )
                    self._trade_journal.append_row(
                        build_trade_csv_row(
                            outcome=outcome,
                            pair=pair,
                            signal=signal,
                            event_mono=time.monotonic(),
                            config_demo=self.config.demo,
                            config_dry_run=signed_dry_run,
                            config_simulation=self.config.simulation,
                            production_binance=self._production_binance,
                            min_score=self.config.min_score,
                            tick_seconds=self.config.tick_seconds,
                            ctx=ctx,
                            arb_record=arb_record,
                            tm=tm,
                            cumulative_session_pnl=self._cumulative_arb_pnl,
                            balance_verify=balance_verify,
                            error_message=(ctx.error or "")
                            if ctx.state != ExecutorState.DONE
                            else "",
                        ),
                    )
                if not self.running:
                    break
        finally:
            # Always emit the per-pair structured console line, even when
            # the loop exited early or an exception bubbled.
            for pair in self.config.pairs:
                signal_for_line, sent_for_line = pair_status.get(pair, (None, "NO reason=skipped"))
                snapshot_for_line = self.generator.last_snapshot.get(pair)
                logger.info(
                    "%s",
                    format_dryrun_console_line(
                        pair=pair,
                        snapshot=snapshot_for_line,
                        signal=signal_for_line,
                        sent=sent_for_line,
                    ),
                )
            if self.config.demo and isinstance(self.exchange, MockExchange):
                self.exchange.advance_demo_script()

        self.health.touch_heartbeat()
        self.health.circuit_breaker_open = self.executor.circuit_breaker.is_open()
        self.health.current_capital = estimate_inventory_usd(self.inventory)
        self.health.daily_pnl = self.risk_manager.daily_realized_pnl

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    async def verify_balances_post_trade(self, pair: str) -> str:
        """Re-fetch balances and compare to :class:`InventoryTracker` (post ``_sync_balances``).

        Returns ``skipped``, ``ok``, ``mismatch_cex``, ``mismatch_wallet``, or ``fetch_error``.
        """
        if not _balance_verify_enabled(demo=self.config.demo):
            return "skipped"
        if self.config.dry_run:
            return "skipped"
        tol = _balance_tolerance()
        try:
            cex_raw = self.exchange.fetch_balance()
        except Exception as exc:
            logger.warning("Balance verify skipped (CEX fetch failed): %s", exc)
            return "fetch_error"
        wallet_raw = self._fetch_wallet_balances() or {}
        parts = pair.upper().split("/")
        if len(parts) != 2:
            return "skipped"
        base, quote = parts[0], parts[1]

        for asset in (base, quote):
            expected = self.inventory.get_available(Venue.BINANCE, asset)
            actual = _cex_free_decimal(cex_raw, asset)
            diff = abs(expected - actual)
            if diff > tol:
                msg = (
                    f"BALANCE MISMATCH CEX {asset}: tracker={expected} "
                    f"actual_free={actual} diff={diff} tol={tol}"
                )
                logger.critical(msg)
                if self._telegram.enabled:
                    self._telegram.send(html_escape_text(msg), urgent=True)
                self.stop()
                return "mismatch_cex"

        for asset_key, raw_amt in wallet_raw.items():
            ak = str(asset_key).upper()
            if ak not in (base, quote):
                continue
            expected_w = self.inventory.get_available(Venue.WALLET, ak)
            actual_w = to_decimal(raw_amt)
            diff_w = abs(expected_w - actual_w)
            if diff_w > tol:
                msg = (
                    f"BALANCE MISMATCH WALLET {ak}: tracker={expected_w} "
                    f"actual={actual_w} diff={diff_w} tol={tol}"
                )
                logger.critical(msg)
                if self._telegram.enabled:
                    self._telegram.send(html_escape_text(msg), urgent=True)
                self.stop()
                return "mismatch_wallet"

        return "ok"

    async def _sync_balances(self) -> None:
        cex_balances: Any = None
        cex_fetch_failed = False
        try:
            t0 = time.monotonic()
            cex_balances = self.exchange.fetch_balance()
            dt_ms = int((time.monotonic() - t0) * 1000)
            self.health.cex_connected = True
            self.health.cex_last_response_ms = dt_ms
        except Exception as exc:
            logger.warning("CEX balance sync failed: %s", exc)
            self.health.cex_connected = False
            self.health.cex_last_response_ms = 0
            cex_fetch_failed = True

        # In dry-run, optionally merge ``ARB_VIRTUAL_CEX_BALANCES`` into the
        # dict before it reaches the inventory tracker. Live runs ignore the
        # override entirely so production never operates on synthetic balances.
        cex_overrides_applied: dict[str, Decimal] = {}
        if self.config.dry_run:
            cex_virt_raw = os.getenv(_ENV_VIRTUAL_CEX_BALANCES, "").strip()
            if cex_virt_raw:
                cex_overrides_applied = _parse_virtual_balances(cex_virt_raw)
                if cex_overrides_applied:
                    cex_balances = _apply_cex_virtual_overrides(
                        cex_balances or {}, cex_overrides_applied
                    )
                    logger.info(
                        "ARB_VIRTUAL_CEX_BALANCES override active (dry-run): %s",
                        ",".join(f"{k}={v}" for k, v in cex_overrides_applied.items()),
                    )
                    # Override gives us a usable inventory even if the live fetch
                    # failed; surface that so the per-tick line stops blaming
                    # ``no_opportunity`` on a missing CEX connection.
                    if cex_fetch_failed:
                        self.health.cex_connected = True

        if cex_balances is not None:
            try:
                self.inventory.update_from_cex(Venue.BINANCE, cex_balances)
            except Exception as exc:
                logger.warning("inventory.update_from_cex failed: %s", exc)

        wallet_balances = self._fetch_wallet_balances()
        if wallet_balances:
            self.inventory.update_from_wallet(Venue.WALLET, wallet_balances)

    def _fetch_wallet_balances(self) -> dict[str, Decimal]:
        """Return on-chain balances; in demo / no-chain mode we synthesize them.

        For live runs we read native ETH **and** the ERC20 ``balanceOf(wallet)``
        for every token referenced by a loaded pool on
        :attr:`pricing_engine`. Token symbols are normalized via
        :data:`_WALLET_SYMBOL_ALIASES` so that wrapped tokens line up with the
        CEX-style ticker the inventory tracker stores (e.g. ``WETH`` → ``ETH``).

        When the bot runs with ``--dry-run`` and ``ARB_VIRTUAL_BALANCES`` is set
        (e.g. ``ETH=2,USDC=5000``) the parsed values **override** the on-chain
        readings so the dry-run pipeline can be exercised without funding the
        wallet. The override is ignored outside dry-run for safety.
        """
        if self.config.demo and isinstance(self.exchange, MockExchange):
            return self.exchange.wallet_balances_for_demo()
        if self.config.demo:
            return {
                "ETH": DEMO_WALLET_BALANCE_BASE,
                "BTC": DEMO_WALLET_BALANCE_BTC,
                "USDT": DEMO_WALLET_BALANCE_QUOTE,
            }

        balances: dict[str, Decimal] = {}
        chain_balances_ok = False

        if self.chain_client is not None:
            wallet_addr = os.getenv("ARB_WALLET_ADDRESS", "").strip()
            if wallet_addr:
                try:
                    from core.types import Address

                    addr = Address.from_string(wallet_addr)
                    try:
                        eth = self.chain_client.get_balance(addr)
                        balances["ETH"] = to_decimal(eth.human)
                        chain_balances_ok = True
                    except Exception as exc:
                        logger.warning("native ETH balance fetch failed: %s", exc)

                    pool_tokens: list[Any] = []
                    if self.pricing_engine is not None:
                        seen: set[str] = set()
                        for pool in self.pricing_engine.pools.values():
                            for tok in (pool.token0, pool.token1):
                                key = tok.address.lower
                                if key in seen:
                                    continue
                                seen.add(key)
                                pool_tokens.append(tok)

                    for tok in pool_tokens:
                        try:
                            amt = self.chain_client.get_erc20_balance(tok.address, addr)
                            sym_norm = _WALLET_SYMBOL_ALIASES.get(
                                tok.symbol.upper(), tok.symbol.upper()
                            )
                            human = to_decimal(amt.human)
                            existing = balances.get(sym_norm, Decimal("0"))
                            # Native ETH already filled the "ETH" slot when we
                            # alias WETH -> ETH; keep the larger of the two so
                            # gas-only ETH does not mask a WETH position.
                            if sym_norm == "ETH" and existing > 0 and human <= 0:
                                continue
                            if sym_norm == "ETH":
                                balances[sym_norm] = max(existing, human)
                            else:
                                balances[sym_norm] = human
                            chain_balances_ok = True
                        except Exception as exc:
                            logger.warning(
                                "wallet ERC20 balance fetch failed for %s (%s): %s",
                                tok.symbol,
                                tok.address.checksum,
                                exc,
                            )
                except Exception as exc:
                    logger.warning("wallet balance fetch failed: %s", exc)

        if self.config.dry_run:
            virt_raw = os.getenv(_ENV_VIRTUAL_BALANCES, "").strip()
            if virt_raw:
                overrides = _parse_virtual_balances(virt_raw)
                if overrides:
                    balances.update(overrides)
                    logger.info(
                        "ARB_VIRTUAL_BALANCES override active (dry-run): %s",
                        ",".join(f"{k}={v}" for k, v in overrides.items()),
                    )
                    chain_balances_ok = True

        if not chain_balances_ok:
            return {}
        return balances


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
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log DRY_RUN lines and skip execution (also ARB_DRY_RUN=1)",
    )
    args = p.parse_args(argv)
    pairs = list(args.pairs)
    if args.demo and pairs == [DEFAULT_PAIR]:
        pairs = ["ETH/USDT", "BTC/USDT"]
    max_ts = Decimal(args.max_trade_size) if args.max_trade_size is not None else None
    dry_run = bool(args.dry_run) or _env_truthy("ARB_DRY_RUN")
    dry_run_mode_raw = (os.getenv(_ENV_DRY_RUN_MODE, "") or "").strip().lower()
    dry_run_mode = dry_run_mode_raw or _DRY_RUN_MODE_LOG
    return ArbBotConfig(
        demo=args.demo,
        pairs=pairs,
        min_score=Decimal(args.min_score),
        simulation=not args.live,
        tick_seconds=args.tick,
        rpc_url=os.getenv("ETH_RPC_URL") or os.getenv("RPC_ENDPOINT") or None,
        max_trade_size=max_ts,
        max_signals_per_tick=max(1, args.max_signals_per_tick),
        dry_run=dry_run,
        dry_run_mode=dry_run_mode,
    )


def main(argv: Optional[list[str]] = None) -> None:
    # Load repo-root .env before ArbBot reads TELEGRAM_* / keys. (Importing
    # config.config later also calls load_dotenv, but TelegramNotifier is
    # constructed first — without this, .env Telegram vars were never seen.)
    load_dotenv(_ROOT / ".env")
    configure_arb_bot_logging()
    random.seed(0)  # deterministic demo output
    bot: ArbBot | None = None
    try:
        cfg = _parse_args(argv)
        bot = ArbBot(cfg)
        if bot._telegram.enabled:
            logger.info("Telegram alerts enabled")
        elif bot._telegram_controls_enabled:
            logger.warning(
                "TELEGRAM_CONTROLS_ENABLED but Telegram token/chat missing — "
                "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
            )
        else:
            tok = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
            cid = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
            if tok ^ cid:
                logger.warning(
                    "Telegram misconfigured: set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
                    "(one is missing)",
                )
            else:
                logger.info(
                    "Telegram alerts off — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable",
                )
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        if bot is not None:
            bot.stop()
        logger.info("Bot stopped by user")
    finally:
        if bot is not None:
            bot.on_shutdown()


if __name__ == "__main__":
    main()
