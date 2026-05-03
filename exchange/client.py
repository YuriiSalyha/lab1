"""
CCXT spot exchange client (Binance / Bybit testnet) with weight-based rate limiting,
logging, and normalized Decimal outputs for monetary fields.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypeVar

import ccxt
from ccxt.base.errors import (
    AuthenticationError,
    ExchangeError,
    ExchangeNotAvailable,
    NetworkError,
    RequestTimeout,
)

from exchange.rate_limiter import WeightRateLimiter
from strategy.fees import cex_taker_bps_from_ccxt_ratio

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Allowlisted ccxt exchange ids (lowercase constructor names).
SUPPORTED_CCXT_IDS = frozenset({"binance", "bybit"})

# Non-Binance exchanges: conservative fixed weight for local rate limiter (no Binance IP headers).
BYBIT_FETCH_ORDERBOOK_WEIGHT = 1

# Binance spot GET /api/v3/depth weight by limit (approximate; see Binance API docs).
_ORDERBOOK_WEIGHT_BY_LIMIT = (
    (100, 5),
    (500, 25),
    (1000, 50),
    (5000, 250),
)


def orderbook_request_weight(limit: int) -> int:
    """Return REST weight for a depth request with the given ``limit``."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    for hi, w in _ORDERBOOK_WEIGHT_BY_LIMIT:
        if limit <= hi:
            return w
    return 250


def orderbook_request_weight_for_exchange(exchange_id: str, limit: int) -> int:
    """REST weight for ``fetch_order_book`` (Binance table; other venues use a fixed weight)."""
    if exchange_id == "binance":
        return orderbook_request_weight(limit)
    if exchange_id in SUPPORTED_CCXT_IDS:
        return BYBIT_FETCH_ORDERBOOK_WEIGHT
    raise ValueError(f"unsupported exchange_id: {exchange_id!r}")


class ExchangeClient:
    """
    Wrapper around ccxt (Binance / Bybit spot testnet by default).
    Handles rate limiting, error handling, and response normalization.
    """

    def __init__(
        self,
        config: dict,
        *,
        exchange_id: str = "binance",
        rate_limit_max_weight: int | None = None,
        rate_limit_window_sec: float | None = None,
    ) -> None:
        """
        Initialize with CCXT config (apiKey, secret, sandbox, options, ...).

        ``exchange_id``: ``"binance"`` or ``"bybit"`` (see ``SUPPORTED_CCXT_IDS``).

        Optional keys in ``config``:

        - ``rateLimitMaxWeight`` (default 1200): max weight per window for the
          local :class:`WeightRateLimiter` (Binance IP budget is often 1200/min).
        - ``rateLimitWindowSec`` (default 60): sliding window length in seconds.

        Constructor keyword arguments override those keys.
        """
        eid = exchange_id.lower().strip()
        if eid not in SUPPORTED_CCXT_IDS:
            raise ValueError(
                f"exchange_id must be one of {sorted(SUPPORTED_CCXT_IDS)}, got {exchange_id!r}",
            )

        cfg = copy.deepcopy(config)
        max_w = rate_limit_max_weight
        if max_w is None:
            max_w = int(cfg.pop("rateLimitMaxWeight", 1200))
        win = rate_limit_window_sec
        if win is None:
            win = float(cfg.pop("rateLimitWindowSec", 60.0))

        factory = getattr(ccxt, eid)
        self._exchange_id = eid
        self.client = factory(cfg)
        self.client.enableRateLimit = True
        self.client.enableLastResponseHeaders = True

        self._rate_limiter = WeightRateLimiter(max_weight=max_w, window_sec=win)

        self._health_check()

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @staticmethod
    def to_decimal(v: Any) -> Decimal:
        if v is None:
            return Decimal("0")
        return Decimal(str(v))

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not symbol or not isinstance(symbol, str):
            raise ValueError("symbol must be a non-empty string")
        if "/" not in symbol:
            raise ValueError("symbol must be unified form, e.g. ETH/USDT")

    @staticmethod
    def _validate_side(side: str) -> None:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")

    @staticmethod
    def _validate_positive(name: str, value: float | Decimal | str | int) -> Decimal:
        d = ExchangeClient.to_decimal(value)
        if d <= 0:
            raise ValueError(f"{name} must be positive")
        return d

    def _health_check(self) -> None:
        """Verify connectivity (public ``fetchTime``-style endpoint)."""
        try:
            server_ms = self._ccxt_request("fetch_time", 1, self.client.fetch_time)
        except (NetworkError, RequestTimeout, ExchangeNotAvailable) as e:
            logger.error("health check failed (network): %s", e)
            raise
        except AuthenticationError:
            logger.exception("health check: authentication error")
            raise
        except ExchangeError as e:
            logger.error("health check failed: %s", e)
            raise
        logger.info("exchange health check ok (server time ms=%s)", server_ms)

    def _log_used_weight_header(self) -> None:
        headers = getattr(self.client, "last_response_headers", None) or {}
        for key, val in headers.items():
            if isinstance(key, str) and "used-weight" in key.lower():
                logger.debug("Binance weight header %s=%s", key, val)

    def _summarize_response(self, operation: str, result: Any) -> str:
        if operation == "fetch_order_book":
            if not isinstance(result, dict):
                return repr(result)[:500]
            return (
                f"bids={len(result.get('bids') or [])} "
                f"asks={len(result.get('asks') or [])} "
                f"ts={result.get('timestamp')}"
            )
        if operation == "fetch_balance":
            if not isinstance(result, dict):
                return repr(result)[:500]
            skip = frozenset({"info", "free", "used", "total", "timestamp", "datetime", "debt"})
            n = sum(1 for k, v in result.items() if k not in skip and isinstance(v, dict))
            return f"assets_with_balance={n}"
        if operation in (
            "create_limit_ioc_order",
            "create_market_order",
            "cancel_order",
            "fetch_order",
        ):
            if not isinstance(result, dict):
                return repr(result)[:500]
            return (
                f"id={result.get('id')} status={result.get('status')} "
                f"filled={result.get('filled')}"
            )
        if operation == "fetch_trading_fee":
            if not isinstance(result, dict):
                return repr(result)[:500]
            return f"maker={result.get('maker')} taker={result.get('taker')}"
        if operation == "fetch_time":
            return f"server_time_ms={result}"
        return repr(result)[:500]

    # High-frequency, low-information requests stay at DEBUG so the per-tick
    # console output is dominated by signal/trade lines, not raw RPC chatter.
    # Other ops (orders, balance, fee, time) keep their INFO level for audit.
    _QUIET_OPS = frozenset({"fetch_order_book", "fetch_ticker"})

    def _ccxt_request(self, operation: str, weight: int, fn: Callable[[], T]) -> T:
        self._rate_limiter.acquire(weight)
        op_log_level = logging.DEBUG if operation in self._QUIET_OPS else logging.INFO
        logger.log(op_log_level, "%s: executing (weight=%s)", operation, weight)
        try:
            result = fn()
        except AuthenticationError as e:
            logger.error("%s: authentication error: %s", operation, e)
            raise
        except (NetworkError, RequestTimeout, ExchangeNotAvailable) as e:
            logger.error("%s: network error: %s", operation, e)
            raise
        except ExchangeError as e:
            logger.error("%s: exchange error: %s", operation, e)
            raise

        self._log_used_weight_header()
        logger.log(
            op_log_level,
            "%s: ok — %s",
            operation,
            self._summarize_response(operation, result),
        )
        return result

    def _parse_fee(self, raw: dict) -> tuple[Decimal, str | None]:
        fee = raw.get("fee")
        if isinstance(fee, dict):
            return self.to_decimal(fee.get("cost")), fee.get("currency")
        if fee is not None:
            return self.to_decimal(fee), None
        fees = raw.get("fees") or []
        if isinstance(fees, list) and fees:
            first = fees[0]
            if isinstance(first, dict):
                return self.to_decimal(first.get("cost")), first.get("currency")
        return Decimal("0"), None

    def _normalize_order(self, raw: dict) -> dict:
        fee_cost, fee_asset = self._parse_fee(raw)
        avg = raw.get("average")
        avg_d: Decimal | None
        if avg is None:
            avg_d = None
        else:
            avg_d = self.to_decimal(avg)

        tif = raw.get("timeInForce") or raw.get("time_in_force")
        ts = raw.get("timestamp")
        return {
            "id": str(raw.get("id", "")),
            "symbol": raw.get("symbol"),
            "side": raw.get("side"),
            "type": raw.get("type"),
            "time_in_force": tif,
            "amount_requested": self.to_decimal(raw.get("amount")),
            "amount_filled": self.to_decimal(raw.get("filled")),
            "avg_fill_price": avg_d if avg_d is not None else Decimal("0"),
            "fee": fee_cost,
            "fee_asset": fee_asset,
            "status": raw.get("status"),
            "timestamp": int(ts) if ts is not None else 0,
        }

    def fetch_order_book(
        self,
        symbol: str,
        limit: int = 20,
    ) -> dict:
        """
        Fetch L2 order book snapshot.

        Returns normalized dict:
        {
            'symbol': 'ETH/USDT',
            'timestamp': 1706000000000,
            'bids': [(price, qty), ...],  # Sorted bid: high→low
            'asks': [(price, qty), ...],  # Sorted ask: low→high
            'best_bid': (price, qty) | None,
            'best_ask': (price, qty) | None,
            'mid_price': Decimal | None, # None if bids or asks missing
            'spread_bps': Decimal | None, # (ask − bid) / mid × 10_000; None if one side empty
        }
        """
        self._validate_symbol(symbol)
        if not isinstance(limit, int) or limit < 1 or limit > 5000:
            raise ValueError("limit must be an int between 1 and 5000")

        w = orderbook_request_weight_for_exchange(self._exchange_id, limit)
        raw = self._ccxt_request(
            "fetch_order_book",
            w,
            lambda: self.client.fetch_order_book(symbol, limit),
        )

        bids_raw = raw.get("bids") or []
        asks_raw = raw.get("asks") or []

        def level(row: list | tuple) -> tuple[Decimal, Decimal]:
            price, qty = row[0], row[1]
            return (self.to_decimal(price), self.to_decimal(qty))

        bids = [level(r) for r in bids_raw]
        asks = [level(r) for r in asks_raw]

        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0], reverse=False)

        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None

        mid_price: Decimal | None = None
        spread_bps: Decimal | None = None
        if best_bid is not None and best_ask is not None:
            bp, _ = best_bid
            ap, _ = best_ask
            mid_price = (bp + ap) / Decimal("2")
            if mid_price > 0:
                # Full spread as basis points of mid: (ask − bid) / mid × 10_000
                spread_bps = (ap - bp) / mid_price * Decimal("10000")

        nonce = raw.get("nonce")
        last_update_id: int | None
        if nonce is None:
            last_update_id = None
        else:
            try:
                last_update_id = int(nonce)
            except (TypeError, ValueError):
                last_update_id = None

        return {
            "symbol": raw.get("symbol", symbol),
            "timestamp": raw.get("timestamp"),
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread_bps": spread_bps,
            # WebSocket sync: sequence when ccxt exposes it (e.g. Binance lastUpdateId).
            "last_update_id": last_update_id,
            "nonce": nonce,
        }

    def fetch_balance(self) -> dict[str, dict]:
        """
        Fetch account balances.

        Returns:
        {
            'ETH':  {'free': Decimal(...), 'locked': Decimal(...), 'total': Decimal(...)},
            ...
        }

        Zero-balance assets are excluded.
        """
        raw = self._ccxt_request("fetch_balance", 10, lambda: self.client.fetch_balance())
        skip = frozenset({"info", "free", "used", "total", "timestamp", "datetime", "debt"})
        out: dict[str, dict] = {}

        for currency, amounts in raw.items():
            if currency in skip or not isinstance(amounts, dict):
                continue
            free = self.to_decimal(amounts.get("free"))
            locked = self.to_decimal(amounts.get("used"))
            total_raw = amounts.get("total")
            total = free + locked if total_raw is None else self.to_decimal(total_raw)
            if total == 0:
                continue
            out[currency] = {
                "free": free,
                "locked": locked,
                "total": total,
            }
        return out

    def create_limit_ioc_order(
        self,
        symbol: str,  # "ETH/USDT"
        side: str,  # "buy" or "sell"
        amount: float,  # Quantity of base asset
        price: float,  # Limit price
    ) -> dict:
        """
        Place a LIMIT IOC (Immediate Or Cancel) order.

        Returns normalized order result (amounts as Decimal).
        """
        self._validate_symbol(symbol)
        self._validate_side(side)
        amt = self._validate_positive("amount", amount)
        prc = self._validate_positive("price", price)

        raw = self._ccxt_request(
            "create_limit_ioc_order",
            1,
            lambda: self.client.create_order(
                symbol,
                "limit",
                side,
                float(amt),
                float(prc),
                {"timeInForce": "IOC"},
            ),
        )
        return self._normalize_order(raw)

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
    ) -> dict:
        """Place a market order. Same normalized return shape as limit IOC."""
        self._validate_symbol(symbol)
        self._validate_side(side)
        amt = self._validate_positive("amount", amount)

        raw = self._ccxt_request(
            "create_market_order",
            1,
            lambda: self.client.create_order(symbol, "market", side, float(amt)),
        )
        return self._normalize_order(raw)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order; returns normalized order dict."""
        self._validate_symbol(symbol)
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")

        raw = self._ccxt_request(
            "cancel_order",
            1,
            lambda: self.client.cancel_order(order_id, symbol),
        )
        return self._normalize_order(raw)

    def fetch_order_status(self, order_id: str, symbol: str) -> dict:
        """Return normalized order details for an order id."""
        self._validate_symbol(symbol)
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")

        raw = self._ccxt_request(
            "fetch_order",
            4,
            lambda: self.client.fetch_order(order_id, symbol),
        )
        return self._normalize_order(raw)

    def get_trading_fees(self, symbol: str, params: dict | None = None) -> dict:
        """
        Trading fees for one market (CCXT ``fetch_trading_fee``).

        Returns:
            {'maker': Decimal(...), 'taker': Decimal(...)}
        """
        self._validate_symbol(symbol)
        raw = self._ccxt_request(
            "fetch_trading_fee",
            4,
            lambda: self.client.fetch_trading_fee(symbol, params or {}),
        )
        return {
            "maker": self.to_decimal(raw.get("maker")),
            "taker": self.to_decimal(raw.get("taker")),
        }

    def max_taker_fee_bps_for_symbols(self, symbols: list[str]) -> Decimal | None:
        """Return the maximum CCXT **taker** fee in bps across ``symbols``.

        Uses :meth:`get_trading_fees` per symbol. Returns ``None`` if every lookup
        fails or yields a non-positive taker ratio. Ratios above **5%** (500 bps)
        per symbol are ignored as bad data.
        """
        if not symbols:
            return None
        cap_bps = Decimal("500")
        best: Decimal | None = None
        for sym in symbols:
            try:
                row = self.get_trading_fees(sym)
                taker = row.get("taker")
                if taker is None:
                    continue
                td = self.to_decimal(taker)
                if td <= 0:
                    logger.warning("CEX taker fee missing or zero for %s", sym)
                    continue
                bps = cex_taker_bps_from_ccxt_ratio(td)
                if bps > cap_bps:
                    logger.warning(
                        "CEX taker bps implausibly high for %s (%s > %s), skipping",
                        sym,
                        bps,
                        cap_bps,
                    )
                    continue
                best = bps if best is None else max(best, bps)
            except Exception as exc:
                logger.warning("fetch_trading_fee failed for %s: %s", sym, exc)
                continue
        return best
