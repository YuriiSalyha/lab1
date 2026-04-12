"""Tests for :mod:`pricing.historical_price_impact`."""

from unittest.mock import MagicMock

import requests
from eth_abi import encode as abi_encode
from web3 import Web3

from chain.uniswap_v2_events import UNISWAP_V2_SYNC_TOPIC0
from core.types import Address, Token
from pricing.historical_price_impact import (
    ReserveSnapshot,
    fetch_sync_snapshots,
    series_impact_for_sizes,
    snapshot_from_log,
)
from pricing.uniswap_v2_pair import UniswapV2Pair

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def _pair() -> UniswapV2Pair:
    t0 = Token(address=A1, symbol="AAA", decimals=18)
    t1 = Token(address=A2, symbol="BBB", decimals=18)
    return UniswapV2Pair(PAIR, t0, t1, 10**24, 10**24, 30)


def test_snapshot_from_log() -> None:
    r0, r1 = 10**23, 10**22
    data = abi_encode(["uint112", "uint112"], [r0, r1])
    log = {
        "address": PAIR.checksum,
        "topics": [UNISWAP_V2_SYNC_TOPIC0],
        "data": "0x" + data.hex(),
        "blockNumber": 99,
        "logIndex": 3,
    }
    s = snapshot_from_log(log)
    assert s == ReserveSnapshot(99, 3, r0, r1)


def test_series_impact_for_sizes_two_snapshots() -> None:
    p = _pair()
    t0 = p.token0
    snaps = [
        ReserveSnapshot(1, 0, 10**24, 10**24),
        ReserveSnapshot(2, 0, 10**23, 10**24),
    ]
    rows = series_impact_for_sizes(snaps, p, t0, [10**20])
    assert len(rows) == 2
    assert rows[0]["block_number"] == 1
    assert rows[1]["block_number"] == 2
    assert 10**20 in rows[0]["impact_pct_by_amount"]
    # Skewed reserves in second row → different impact
    assert rows[0]["impact_pct_by_amount"][10**20] != rows[1]["impact_pct_by_amount"][10**20]


def test_fetch_sync_snapshots_chunks_get_logs() -> None:
    """Alchemy-style providers reject wide ranges; we query in chunks."""
    w3 = MagicMock()
    w3.eth.get_logs = MagicMock(return_value=[])
    fetch_sync_snapshots(w3, PAIR, 100, 125, chunk_blocks=10)
    assert w3.eth.get_logs.call_count == 3
    spans = [(c[0][0]["fromBlock"], c[0][0]["toBlock"]) for c in w3.eth.get_logs.call_args_list]
    assert spans[0] == (Web3.to_hex(100), Web3.to_hex(109))
    assert spans[1] == (Web3.to_hex(110), Web3.to_hex(119))
    assert spans[2] == (Web3.to_hex(120), Web3.to_hex(125))


def test_fetch_sync_snapshots_halves_span_on_http_400() -> None:
    """Oversized first request (e.g. --chunk-blocks 50000 on Alchemy free) → retry smaller."""
    err = requests.HTTPError()
    err.response = MagicMock(status_code=400)
    w3 = MagicMock()
    calls: list[dict] = []

    def get_logs(flt: dict) -> list:
        calls.append(flt)
        if len(calls) == 1:
            raise err
        return []

    w3.eth.get_logs = MagicMock(side_effect=get_logs)
    fetch_sync_snapshots(w3, PAIR, 100, 125, chunk_blocks=50_000)
    assert len(calls) >= 2
    w0 = int(calls[0]["fromBlock"], 16)
    w1 = int(calls[0]["toBlock"], 16)
    assert w1 - w0 + 1 == 26
    w0b = int(calls[1]["fromBlock"], 16)
    w1b = int(calls[1]["toBlock"], 16)
    assert w0b == 100
    assert w1b - w0b + 1 == 13
