"""Tests for :mod:`chain.uniswap_v2_events`."""

from eth_abi import encode as abi_encode

from chain.decoder import TransactionDecoder
from chain.uniswap_v2_events import (
    UNISWAP_V2_SYNC_TOPIC0,
    reserves_from_sync_parse_result,
    sync_logs_filter,
)


def test_sync_logs_filter_shape() -> None:
    f = sync_logs_filter("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    assert f["topics"] == [UNISWAP_V2_SYNC_TOPIC0]
    assert f["address"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def test_reserves_from_sync_parse_result() -> None:
    r0, r1 = 10**22, 10**20
    data = abi_encode(["uint112", "uint112"], [r0, r1])
    log = {
        "address": "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
        "topics": [UNISWAP_V2_SYNC_TOPIC0],
        "data": "0x" + data.hex(),
    }
    ev = TransactionDecoder.parse_event(log)
    assert reserves_from_sync_parse_result(ev) == (r0, r1)


def test_reserves_from_sync_rejects_zero() -> None:
    data = abi_encode(["uint112", "uint112"], [0, 10**18])
    log = {
        "address": "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
        "topics": [UNISWAP_V2_SYNC_TOPIC0],
        "data": "0x" + data.hex(),
    }
    ev = TransactionDecoder.parse_event(log)
    assert reserves_from_sync_parse_result(ev) is None
