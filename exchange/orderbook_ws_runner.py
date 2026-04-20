"""
Async WebSocket order book: REST snapshot + incremental updates, optional resync on gaps.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from exchange.client import ExchangeClient
from exchange.local_l2_book import SIDE_ASK, SIDE_BID, LocalL2Book
from exchange.ws_depth_adapters import (
    DepthEvent,
    parse_binance_depth_json,
    parse_bybit_orderbook_json,
)

logger = logging.getLogger(__name__)

# Binance Spot testnet streams
BINANCE_TESTNET_WS_BASE = "wss://stream.testnet.binance.vision/ws"
BINANCE_MAINNET_WS_BASE = "wss://stream.binance.com:9443/ws"

# Bybit v5 public spot testnet.
BYBIT_TESTNET_SPOT_WS = "wss://stream-testnet.bybit.com/v5/public/spot"
BYBIT_MAINNET_SPOT_WS = "wss://stream.bybit.com/v5/public/spot"

# Order book depth levels for Bybit WS topic ``orderbook.{DEPTH}.{SYMBOL}``.
BYBIT_WS_ORDERBOOK_DEPTH = 50

# Ping interval for Bybit (seconds); server may disconnect idle clients.
BYBIT_PING_INTERVAL_SEC = 20.0

# Reconnect backoff.
WS_RECONNECT_INITIAL_SEC = 1.0
WS_RECONNECT_MAX_SEC = 60.0


def unified_to_binance_stream_symbol(symbol: str) -> str:
    """``ETH/USDT`` → ``ethusdt``."""
    return symbol.replace("/", "").lower()


def unified_to_bybit_linear_symbol(symbol: str) -> str:
    """``ETH/USDT`` → ``ETHUSDT``."""
    return symbol.replace("/", "").upper()


def binance_depth_stream_url(*, symbol: str, testnet: bool) -> str:
    su = unified_to_binance_stream_symbol(symbol)
    base = BINANCE_TESTNET_WS_BASE if testnet else BINANCE_MAINNET_WS_BASE
    return f"{base}/{su}@depth"


def bybit_spot_ws_url(client: ExchangeClient) -> str:
    """Public spot WS URL for current ccxt environment."""
    sandbox = getattr(client.client, "sandbox", None)
    if sandbox is None:
        sandbox = False
    return BYBIT_TESTNET_SPOT_WS if sandbox else BYBIT_MAINNET_SPOT_WS


class BinanceDepthSync:
    """Track Binance ``depthUpdate`` alignment with REST ``lastUpdateId`` (``L``)."""

    def __init__(self, snapshot_l: int | None) -> None:
        self._L = snapshot_l
        self._synced = snapshot_l is None
        self._last_final: int | None = None

    @property
    def synced(self) -> bool:
        return self._synced

    def evaluate(self, ev: DepthEvent) -> str:
        """
        Returns ``apply``, ``skip``, or ``resync``.
        """
        if ev.u_final is None:
            return "apply" if self._synced else "skip"
        u_f = ev.u_final
        u_0 = ev.u_first
        if not self._synced:
            if self._L is None:
                self._synced = True
                self._last_final = u_f
                return "apply"
            if u_f < self._L:
                return "skip"
            if u_0 is not None and self._L is not None and u_0 <= self._L <= u_f:
                self._synced = True
                self._last_final = u_f
                return "apply"
            if u_0 is not None and self._L is not None and u_0 > self._L:
                return "resync"
            return "skip"
        if u_0 is not None and self._last_final is not None:
            if u_0 < self._last_final:
                return "skip"
            if u_0 > self._last_final + 1:
                return "resync"
        self._last_final = u_f
        return "apply"


class BybitSeqSync:
    """Track Bybit orderbook ``seq`` monotonicity."""

    def __init__(self) -> None:
        self._last: int | None = None

    def evaluate_snapshot(self, ev: DepthEvent) -> str:
        self._last = ev.seq
        return "apply"

    def evaluate_delta(self, ev: DepthEvent) -> str:
        if ev.seq is None:
            return "apply"
        if self._last is None:
            self._last = ev.seq
            return "apply"
        if ev.seq <= self._last:
            return "skip"
        if ev.seq > self._last + 1:
            return "resync"
        self._last = ev.seq
        return "apply"


def seed_book_from_rest(
    client: ExchangeClient,
    symbol: str,
    limit: int,
) -> tuple[LocalL2Book, dict]:
    ob = client.fetch_order_book(symbol, limit=limit)
    book = LocalL2Book()
    book.apply_snapshot(
        ob["bids"],
        ob["asks"],
        sequence_id=ob.get("last_update_id"),
    )
    return book, ob


def _apply_depth_event_binance(book: LocalL2Book, ev: DepthEvent) -> None:
    if ev.bids:
        book.apply_delta([(p, q) for p, q in ev.bids], SIDE_BID)
    if ev.asks:
        book.apply_delta([(p, q) for p, q in ev.asks], SIDE_ASK)


def _apply_depth_event_bybit(
    book: LocalL2Book,
    ev: DepthEvent,
    *,
    is_snapshot: bool,
) -> None:
    if is_snapshot:
        bids = [(p, q) for p, q in ev.bids]
        asks = [(p, q) for p, q in ev.asks]
        book.apply_snapshot(bids, asks, sequence_id=ev.seq)
        return
    if ev.bids:
        book.apply_delta([(p, q) for p, q in ev.bids], SIDE_BID)
    if ev.asks:
        book.apply_delta([(p, q) for p, q in ev.asks], SIDE_ASK)


class OrderBookWsRunner:
    """
    Runs a WebSocket loop: REST seed → stream deltas → invoke callback with normalized book.
    """

    def __init__(
        self,
        client: ExchangeClient,
        symbol: str,
        on_book: Callable[[dict], Awaitable[None] | None],
        *,
        rest_limit: int = 100,
    ) -> None:
        self._client = client
        self._symbol = symbol
        self._on_book = on_book
        self._rest_limit = rest_limit
        self._book: LocalL2Book | None = None
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        eid = self._client.exchange_id
        if eid == "binance":
            await self._run_binance()
        elif eid == "bybit":
            await self._run_bybit()
        else:
            raise ValueError(f"WebSocket runner not implemented for {eid!r}")

    def stop(self) -> None:
        self._stop.set()

    async def _emit(self, ts_ms: int | None) -> None:
        if self._book is None:
            return
        d = self._book.to_normalized_dict(self._symbol, timestamp_ms=ts_ms)
        out = self._on_book(d)
        if inspect.isawaitable(out):
            await out

    async def _run_binance(self) -> None:
        backoff = WS_RECONNECT_INITIAL_SEC
        testnet = getattr(self._client.client, "sandbox", True)
        url = binance_depth_stream_url(symbol=self._symbol, testnet=bool(testnet))
        while not self._stop.is_set():
            book, ob = await asyncio.to_thread(
                seed_book_from_rest,
                self._client,
                self._symbol,
                self._rest_limit,
            )
            self._book = book
            snap_l = ob.get("last_update_id")
            sync = BinanceDepthSync(snap_l)
            await self._emit(ob.get("timestamp") if isinstance(ob.get("timestamp"), int) else None)
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    backoff = WS_RECONNECT_INITIAL_SEC
                    while not self._stop.is_set():
                        raw = await ws.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        ev = parse_binance_depth_json(raw)
                        if ev is None:
                            continue
                        action = sync.evaluate(ev)
                        if action == "resync":
                            logger.warning("binance depth gap — resnapshot")
                            break
                        if action == "skip":
                            continue
                        _apply_depth_event_binance(book, ev)
                        ts = None
                        try:
                            payload = json.loads(raw)
                            pl = payload.get("data", payload)
                            if isinstance(pl, dict) and pl.get("E") is not None:
                                ts = int(pl["E"])
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                        await self._emit(ts)
            except Exception as e:
                logger.exception("binance ws error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(WS_RECONNECT_MAX_SEC, backoff * 2)

    async def _run_bybit(self) -> None:
        backoff = WS_RECONNECT_INITIAL_SEC
        url = bybit_spot_ws_url(self._client)
        sym = unified_to_bybit_linear_symbol(self._symbol)
        topic = f"orderbook.{BYBIT_WS_ORDERBOOK_DEPTH}.{sym}"
        sub_msg = json.dumps({"op": "subscribe", "args": [topic]})
        ping_task: asyncio.Task | None = None

        while not self._stop.is_set():
            book, ob = await asyncio.to_thread(
                seed_book_from_rest,
                self._client,
                self._symbol,
                self._rest_limit,
            )
            self._book = book
            seq_sync = BybitSeqSync()
            await self._emit(ob.get("timestamp") if isinstance(ob.get("timestamp"), int) else None)

            try:
                async with websockets.connect(
                    url,
                    ping_interval=None,
                ) as ws:
                    await ws.send(sub_msg)
                    backoff = WS_RECONNECT_INITIAL_SEC

                    async def _pinger() -> None:
                        while not self._stop.is_set():
                            await asyncio.sleep(BYBIT_PING_INTERVAL_SEC)
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                            except Exception:
                                return

                    ping_task = asyncio.create_task(_pinger())
                    try:
                        while not self._stop.is_set():
                            raw = await ws.recv()
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="replace")
                            ev = parse_bybit_orderbook_json(raw)
                            if ev is None:
                                continue
                            if ev.kind == "snapshot":
                                seq_sync.evaluate_snapshot(ev)
                                _apply_depth_event_bybit(book, ev, is_snapshot=True)
                                await self._emit(None)
                                continue
                            action = seq_sync.evaluate_delta(ev)
                            if action == "resync":
                                logger.warning("bybit seq gap — resnapshot")
                                break
                            if action == "skip":
                                continue
                            _apply_depth_event_bybit(book, ev, is_snapshot=False)
                            ts = None
                            try:
                                jo = json.loads(raw)
                                if isinstance(jo.get("ts"), (int, float)):
                                    ts = int(jo["ts"])
                            except (json.JSONDecodeError, TypeError, ValueError):
                                pass
                            await self._emit(ts)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                        ping_task = None
            except Exception as e:
                logger.exception("bybit ws error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(WS_RECONNECT_MAX_SEC, backoff * 2)


async def run_orderbook_ws_once(
    client: ExchangeClient,
    symbol: str,
    on_book: Callable[[dict], Awaitable[None] | None],
    *,
    rest_limit: int = 100,
) -> None:
    """Convenience: run until ``stop()`` or process exit."""
    runner = OrderBookWsRunner(client, symbol, on_book, rest_limit=rest_limit)
    await runner.run_forever()
