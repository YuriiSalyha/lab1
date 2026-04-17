"""Tests for :mod:`scripts.arb_checker`."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.types import Address, Token
from exchange.client import ExchangeClient
from inventory.tracker import InventoryTracker, Venue
from pricing.uniswap_v2_pair import UniswapV2Pair
from scripts.arb_checker import ArbChecker, ArbCheckError

PAIR_ADDR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


@pytest.fixture
def eth_usdt_pool() -> UniswapV2Pair:
    usdt = Token(Address("0x1111111111111111111111111111111111111111"), "USDT", 6)
    weth = Token(Address("0x2222222222222222222222222222222222222222"), "WETH", 18)
    return UniswapV2Pair(
        PAIR_ADDR,
        usdt,
        weth,
        reserve0=2_000_000 * 10**6,
        reserve1=1000 * 10**18,
        fee_bps=30,
    )


def _exchange_mock(bid: Decimal, ask: Decimal) -> MagicMock:
    ex = MagicMock(spec=ExchangeClient)
    mid = (bid + ask) / Decimal("2")
    spread_bps = (ask - bid) / mid * Decimal("10000") if mid > 0 else Decimal("0")
    ex.fetch_order_book.return_value = {
        "symbol": "ETH/USDT",
        "timestamp": 1,
        "bids": [],
        "asks": [],
        "best_bid": (bid, Decimal("100")),
        "best_ask": (ask, Decimal("100")),
        "mid_price": mid,
        "spread_bps": spread_bps,
    }
    ex._validate_symbol = ExchangeClient._validate_symbol
    return ex


@pytest.fixture
def mock_exchange() -> MagicMock:
    return _exchange_mock(Decimal("2015"), Decimal("2016"))


def test_check_returns_expected_keys(eth_usdt_pool, mock_exchange):
    pe = SimpleNamespace(pools={PAIR_ADDR: eth_usdt_pool})
    tr = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tr.update_from_cex(Venue.BINANCE, {"ETH": {"free": Decimal("10"), "locked": Decimal("0")}})
    tr.update_from_wallet(Venue.WALLET, {"USDT": Decimal("50000")})

    ac = ArbChecker(pe, mock_exchange, tr, None)
    r = ac.check("ETH/USDT", Decimal("1"))
    assert r["pair"] == "ETH/USDT"
    assert "dex_price" in r and r["dex_price"] > 0
    assert "cex_bid" in r and r["cex_bid"] == Decimal("2015")
    assert "cex_ask" in r and r["cex_ask"] == Decimal("2016")
    assert "gap_bps" in r
    assert r["direction"] in ("buy_dex_sell_cex", "buy_cex_sell_dex")
    assert "estimated_costs_bps" in r
    assert "estimated_net_pnl_bps" in r
    assert "inventory_ok" in r
    assert "executable" in r
    assert "dex_fee_bps" in r["details"]
    assert "gas_cost_usd" in r["details"]


def test_check_raises_without_pools(mock_exchange):
    pe = SimpleNamespace(pools={})
    tr = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    ac = ArbChecker(pe, mock_exchange, tr, None)
    with pytest.raises(ArbCheckError, match="No pools loaded"):
        ac.check("ETH/USDT", Decimal("1"))


def test_inventory_fail_marks_not_executable(eth_usdt_pool, mock_exchange):
    pe = SimpleNamespace(pools={PAIR_ADDR: eth_usdt_pool})
    tr = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tr.update_from_cex(Venue.BINANCE, {"ETH": {"free": Decimal("0"), "locked": Decimal("0")}})
    tr.update_from_wallet(Venue.WALLET, {"USDT": Decimal("1")})

    ac = ArbChecker(pe, mock_exchange, tr, None)
    r = ac.check("ETH/USDT", Decimal("2"))
    assert r["inventory_ok"] is False
    assert r["executable"] is False


def test_integration_executable_when_profitable_and_inventory_ok(eth_usdt_pool):
    """Wide CEX bid vs DEX → positive net bps; both legs funded → executable."""
    ex = _exchange_mock(Decimal("50000"), Decimal("50001"))
    pe = SimpleNamespace(pools={PAIR_ADDR: eth_usdt_pool})
    tr = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tr.update_from_cex(Venue.BINANCE, {"ETH": {"free": Decimal("50"), "locked": Decimal("0")}})
    tr.update_from_wallet(Venue.WALLET, {"USDT": Decimal("500_000")})
    ac = ArbChecker(
        pe,
        ex,
        tr,
        None,
        default_gas_cost_usd=Decimal("0"),
    )
    r = ac.check("ETH/USDT", Decimal("1"))
    assert r["estimated_net_pnl_bps"] > 0
    assert r["inventory_ok"] is True
    assert r["executable"] is True


def test_integration_rejects_when_edge_negative(eth_usdt_pool):
    """Bid too low for buy-DEX path; ask too high for buy-CEX path → both nets negative."""
    ex = _exchange_mock(Decimal("500"), Decimal("100000"))
    pe = SimpleNamespace(pools={PAIR_ADDR: eth_usdt_pool})
    tr = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tr.update_from_cex(Venue.BINANCE, {"ETH": {"free": Decimal("10"), "locked": Decimal("0")}})
    tr.update_from_wallet(Venue.WALLET, {"USDT": Decimal("50000")})
    ac = ArbChecker(pe, ex, tr, None, default_gas_cost_usd=Decimal("0"))
    r = ac.check("ETH/USDT", Decimal("1"))
    assert r["executable"] is False
    assert r["estimated_net_pnl_bps"] <= 0
