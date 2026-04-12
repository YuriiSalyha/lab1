"""Tests for :mod:`pricing.v2_pool_price_feed` (no live WebSocket)."""

from eth_abi import encode as abi_encode

from chain.uniswap_v2_events import UNISWAP_V2_SYNC_TOPIC0
from core.types import Address, Token
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.v2_pool_price_feed import V2PoolPriceFeed

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def _template_pair(r0: int = 10**24, r1: int = 10**24) -> UniswapV2Pair:
    t0 = Token(address=A1, symbol="AAA", decimals=18)
    t1 = Token(address=A2, symbol="BBB", decimals=18)
    return UniswapV2Pair(PAIR, t0, t1, r0, r1, 30)


def _sync_log(r0: int, r1: int, block: int = 12_345, log_index: int = 7) -> dict:
    data = abi_encode(["uint112", "uint112"], [r0, r1])
    return {
        "address": PAIR.checksum,
        "topics": [UNISWAP_V2_SYNC_TOPIC0],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block),
        "logIndex": hex(log_index),
    }


def test_build_tick_decodes_sync_and_spot() -> None:
    template = _template_pair()
    feed = V2PoolPriceFeed("wss://unused", template, lambda _t: None)
    tick = feed._build_tick(_sync_log(2 * 10**24, 10**24))
    assert tick is not None
    assert tick.block_number == 12_345
    assert tick.log_index == 7
    assert tick.reserve0 == 2 * 10**24
    assert tick.reserve1 == 10**24
    assert tick.spot_price_token0 > 0
    assert tick.spot_price_token1 > 0


def test_build_tick_optional_impact_pct() -> None:
    template = _template_pair()
    t0 = template.token0
    feed = V2PoolPriceFeed(
        "wss://unused",
        template,
        lambda _t: None,
        impact_token=t0,
        impact_amounts=[10**18],
    )
    tick = feed._build_tick(_sync_log(10**24, 10**24))
    assert tick is not None
    assert tick.impact_pct_by_amount is not None
    assert 10**18 in tick.impact_pct_by_amount
