"""
Unit tests for ``exchange.client`` and ``exchange.rate_limiter``.

Integration tests (Binance testnet) are marked ``@pytest.mark.integration`` and
skip when the network or credentials are unavailable.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

from exchange.client import (
    ExchangeClient,
    orderbook_request_weight,
    orderbook_request_weight_for_exchange,
)
from exchange.rate_limiter import WeightRateLimiter

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")


@pytest.fixture
def mock_ccxt_binance():
    """Minimal CCXT Binance mock with required attributes."""
    ex = MagicMock()
    ex.fetch_time.return_value = 1_700_000_000_000
    ex.enableRateLimit = True
    ex.enableLastResponseHeaders = True
    ex.last_response_headers = {}
    return ex


@pytest.fixture
def client(mock_ccxt_binance):
    with patch("exchange.client.ccxt.binance", return_value=mock_ccxt_binance):
        ec = ExchangeClient({"apiKey": "test", "secret": "test"})
        ec._ccxt_for_test = mock_ccxt_binance
        return ec


def test_fetch_order_book_structure(client, mock_ccxt_binance):
    """Order book has required fields and correct sort order."""
    mock_ccxt_binance.fetch_order_book.return_value = {
        "symbol": "ETH/USDT",
        "timestamp": 123,
        "bids": [[100.0, 1.0], [99.5, 2.0]],
        "asks": [[101.0, 1.5], [101.5, 1.0]],
    }
    ob = client.fetch_order_book("ETH/USDT", limit=20)
    assert ob["symbol"] == "ETH/USDT"
    assert ob["timestamp"] == 123
    assert set(ob.keys()) >= {
        "symbol",
        "timestamp",
        "bids",
        "asks",
        "best_bid",
        "best_ask",
        "mid_price",
        "spread_bps",
        "last_update_id",
        "nonce",
    }
    assert isinstance(ob["bids"][0][0], Decimal)
    assert isinstance(ob["asks"][0][0], Decimal)


def test_order_book_bids_descending(client, mock_ccxt_binance):
    """Bids sorted highest to lowest."""
    mock_ccxt_binance.fetch_order_book.return_value = {
        "bids": [[99.0, 1.0], [100.0, 2.0], [98.0, 3.0]],
        "asks": [[101.0, 1.0]],
    }
    ob = client.fetch_order_book("ETH/USDT", 20)
    prices = [p for p, _ in ob["bids"]]
    assert prices == sorted(prices, reverse=True)


def test_order_book_asks_ascending(client, mock_ccxt_binance):
    """Asks sorted lowest to highest."""
    mock_ccxt_binance.fetch_order_book.return_value = {
        "bids": [[100.0, 1.0]],
        "asks": [[102.0, 1.0], [101.0, 2.0], [103.0, 1.0]],
    }
    ob = client.fetch_order_book("ETH/USDT", 20)
    prices = [p for p, _ in ob["asks"]]
    assert prices == sorted(prices)


def test_spread_calculation(client, mock_ccxt_binance):
    """Spread = best_ask - best_bid, expressed in bps vs mid."""
    mock_ccxt_binance.fetch_order_book.return_value = {
        "bids": [[100.0, 1.0]],
        "asks": [[100.02, 1.0]],
    }
    ob = client.fetch_order_book("ETH/USDT", 20)
    bp, _ = ob["best_bid"]
    ap, _ = ob["best_ask"]
    mid = ob["mid_price"]
    assert mid is not None
    expected_bps = (ap - bp) / mid * Decimal("10000")
    assert ob["spread_bps"] == expected_bps


def test_fetch_balance_filters_zeros(client, mock_ccxt_binance):
    """Zero-balance assets excluded from result."""
    mock_ccxt_binance.fetch_balance.return_value = {
        "info": {},
        "timestamp": None,
        "datetime": None,
        "ETH": {"free": 0.0, "used": 0.0, "total": 0.0},
        "USDT": {"free": 100.0, "used": 0.0, "total": 100.0},
        "free": {},
        "used": {},
        "total": {},
    }
    bal = client.fetch_balance()
    assert "ETH" not in bal
    assert "USDT" in bal
    assert bal["USDT"]["total"] == Decimal("100")


def test_limit_ioc_returns_fill_info(client, mock_ccxt_binance):
    """IOC order returns fill qty, avg price, fees."""
    mock_ccxt_binance.create_order.return_value = {
        "id": "abc123",
        "symbol": "ETH/USDT",
        "type": "limit",
        "side": "buy",
        "amount": 0.1,
        "filled": 0.1,
        "average": 2500.0,
        "fee": {"cost": 0.0001, "currency": "ETH"},
        "status": "closed",
        "timestamp": 999,
        "timeInForce": "IOC",
    }
    out = client.create_limit_ioc_order("ETH/USDT", "buy", 0.1, 2500.0)
    assert out["amount_filled"] == Decimal("0.1")
    assert out["avg_fill_price"] == Decimal("2500.0")
    assert out["fee"] == Decimal("0.0001")
    assert out["fee_asset"] == "ETH"
    assert out["time_in_force"] == "IOC"


def test_rate_limiter_blocks_when_exhausted():
    """Requests blocked when weight limit reached."""
    lim = WeightRateLimiter(max_weight=2, window_sec=60.0)
    # Advance monotonic after the simulated sleep so the sliding window can clear.
    with patch(
        "exchange.rate_limiter.time.monotonic",
        side_effect=[0.0, 0.0, 0.0, 60.0],
    ):
        lim.acquire(1)
        lim.acquire(1)
        with patch("exchange.rate_limiter.time.sleep") as mock_sleep:
            lim.acquire(1)
            mock_sleep.assert_called()


def test_invalid_symbol_raises():
    with patch("exchange.client.ccxt.binance") as m:
        m.return_value.fetch_time.return_value = 1
        m.return_value.enableRateLimit = True
        m.return_value.enableLastResponseHeaders = True
        c = ExchangeClient({})
        with pytest.raises(ValueError, match="unified"):
            c.fetch_order_book("ETHUSDT", 20)


def test_orderbook_weight_brackets():
    assert orderbook_request_weight(50) == 5
    assert orderbook_request_weight(200) == 25


def test_orderbook_weight_for_exchange_bybit():
    assert orderbook_request_weight_for_exchange("bybit", 500) == 1


def test_exchange_id_rejected():
    with pytest.raises(ValueError, match="exchange_id"):
        ExchangeClient({}, exchange_id="kraken")


@pytest.fixture
def mock_ccxt_bybit():
    ex = MagicMock()
    ex.fetch_time.return_value = 1_700_000_000_000
    ex.enableRateLimit = True
    ex.enableLastResponseHeaders = True
    ex.last_response_headers = {}
    return ex


def test_bybit_client_order_book(mock_ccxt_bybit):
    with patch("exchange.client.ccxt.bybit", return_value=mock_ccxt_bybit):
        c = ExchangeClient({"apiKey": "x", "secret": "y"}, exchange_id="bybit")
        mock_ccxt_bybit.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 1,
            "nonce": 42,
            "bids": [[100.0, 1.0]],
            "asks": [[101.0, 1.0]],
        }
        ob = c.fetch_order_book("ETH/USDT", 20)
        assert ob["last_update_id"] == 42
        assert c.exchange_id == "bybit"


# --- Integration (Binance testnet) ---


@pytest.mark.integration
def test_integration_connects_and_fetches_order_book():
    """ExchangeClient connects to Binance testnet and fetches order books."""
    # Public endpoints only — no API keys required.
    cfg = {
        "sandbox": True,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
    }
    try:
        client = ExchangeClient(cfg)
        ob = client.fetch_order_book("ETH/USDT", limit=5)
    except Exception as e:
        pytest.skip(f"testnet unreachable or misconfigured: {e}")

    assert len(ob["bids"]) >= 1
    assert len(ob["asks"]) >= 1
    assert ob["spread_bps"] is not None


# @pytest.mark.integration
# def test_integration_limit_ioc_place_and_cancel():
#     """
#     Places a LIMIT IOC (unlikely to fill) and cancels a resting limit order.

#     IOC orders that do not rest cannot be canceled; we validate IOC placement,
#     then place a far-from-market GTC-style limit (day) and cancel it to
#     exercise the cancel path on testnet.
#     """
#     key = (os.getenv("BINANCE_TESTNET_API_KEY") or "").strip()
#     sec = (os.getenv("BINANCE_TESTNET_SECRET") or "").strip()
#     if not key or not sec:
#         pytest.skip(
#             "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET not set (use .env in repo root)",
#         )

#     cfg = {
#         "apiKey": key,
#         "secret": sec,
#         "sandbox": True,
#         "options": {"defaultType": "spot"},
#         "enableRateLimit": True,
#     }
#     sym = "ETH/USDT"
#     try:
#         client = ExchangeClient(cfg)
#         xc = client.client
#         xc.load_markets()
#         ob = client.fetch_order_book(sym, limit=5)
#         best_bid = ob["best_bid"][0]
#         best_ask = ob["best_ask"][0]
#         # Sizes/prices must pass Binance MIN_NOTIONAL (tiny IOC notionals used to fail here).
#         amt = float(xc.amount_to_precision(sym, 0.01))
#         ioc_px = float(xc.price_to_precision(sym, float(best_bid * Decimal("0.5"))))
#         gtc_px = float(xc.price_to_precision(sym, float(best_ask * Decimal("0.5"))))
#         # IOC buy well below ask — should not fill; notional still meaningful vs filters.
#         ioc = client.create_limit_ioc_order(sym, "buy", amt, ioc_px)
#         assert ioc["id"]
#         assert isinstance(ioc["amount_requested"], Decimal)

#         # Resting limit order we can cancel (GTC default on Binance spot limit)
#         raw = xc.create_order(
#             sym,
#             "limit",
#             "buy",
#             amt,
#             gtc_px,
#             {"timeInForce": "GTC"},
#         )
#         oid = str(raw["id"])
#         canceled = client.cancel_order(oid, sym)
#         assert str(canceled["id"]) == str(oid)
#     except Exception as e:
#         pytest.skip(f"testnet trading skipped ({type(e).__name__}): {e}")


@pytest.mark.integration
def test_integration_rate_limiter_does_not_break_live_client():
    """Smoke: rate limiter allows normal public calls when budget is large."""
    cfg = {
        "sandbox": True,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
    }
    try:
        client = ExchangeClient(cfg, rate_limit_max_weight=1200)
        for _ in range(3):
            client.fetch_order_book("ETH/USDT", limit=5)
    except Exception as e:
        pytest.skip(f"testnet unreachable: {e}")
