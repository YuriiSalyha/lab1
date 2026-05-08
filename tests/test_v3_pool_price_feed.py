"""Tests for :mod:`pricing.v3_pool_price_feed` (Swap-log decoding + tick build)."""

from __future__ import annotations

from decimal import Decimal

from eth_abi import encode as abi_encode

from core.types import Address, Token
from pricing.v3_pool_price_feed import (
    UNISWAP_V3_SWAP_TOPIC0,
    V3PoolPriceFeed,
    spot_price_from_sqrt_x96,
    v3_swap_logs_filter,
)

POOL = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
TOKEN0 = Token(Address("0x1" + "1" * 39), "WETH", 18)
TOKEN1 = Token(Address("0x2" + "2" * 39), "USDC", 6)


def _addr_topic(addr: Address) -> str:
    return "0x" + ("0" * 24) + addr.lower[2:]


def _make_swap_log(*, sqrt_price_x96: int, tick: int, liquidity: int) -> dict:
    data = abi_encode(
        ["int256", "int256", "uint160", "uint128", "int24"],
        [-1, 1, sqrt_price_x96, liquidity, tick],
    )
    return {
        "address": POOL.checksum,
        "blockNumber": "0x10",
        "logIndex": "0x0",
        "topics": [
            UNISWAP_V3_SWAP_TOPIC0,
            _addr_topic(Address("0x" + "f" * 40)),
            _addr_topic(Address("0x" + "e" * 40)),
        ],
        "data": "0x" + data.hex(),
    }


def test_v3_swap_logs_filter_includes_topic_and_address() -> None:
    flt = v3_swap_logs_filter(POOL.checksum)
    assert flt["address"] == POOL.checksum
    assert flt["topics"] == [UNISWAP_V3_SWAP_TOPIC0]


def test_spot_price_from_sqrt_x96_matches_known_value() -> None:
    # sqrtPriceX96 corresponding to USDC/WETH ≈ 2000 USDC per ETH.
    # price_atom = (sqrt/Q96)^2; with token0=WETH (18), token1=USDC (6):
    # human_t1_per_t0 = price_atom * 10**(18-6) = price_atom * 1e12
    # We want human_t1_per_t0 == 2000 → price_atom == 2e-9 → sqrt_atom ≈ 4.4721e-5
    # → sqrtPriceX96 ≈ 4.4721e-5 * 2**96 ≈ 3.5424e24 ≈ 3543191142285914205922816
    sqrt_x96 = 3_543_191_142_285_914_205_922_816
    t0_per_t1, t1_per_t0 = spot_price_from_sqrt_x96(sqrt_x96, token0_decimals=18, token1_decimals=6)
    # t1_per_t0 ≈ 2000 USDC per WETH (loose tolerance for the demo sqrt picked above).
    assert Decimal("1900") < t1_per_t0 < Decimal("2100")
    assert t0_per_t1 > 0


def test_v3_pool_price_feed_build_tick_decodes_log() -> None:
    received: list = []

    class _PoolStub:
        address = POOL
        token0 = TOKEN0
        token1 = TOKEN1

    feed = V3PoolPriceFeed("ws://x", _PoolStub(), received.append)  # type: ignore[arg-type]
    log = _make_swap_log(
        sqrt_price_x96=3_543_191_142_285_914_205_922_816,
        tick=200_000,
        liquidity=12345,
    )
    tick = feed._build_tick(log)
    assert tick is not None
    assert tick.tick == 200_000
    assert tick.liquidity == 12345
    assert tick.spot_price_token0 > 0
    assert tick.spot_price_token1 > 0
    assert tick.block_number == 0x10


def test_v3_pool_price_feed_ignores_non_v3_swap_log() -> None:
    feed = V3PoolPriceFeed(
        "ws://x",
        type("S", (), {"address": POOL, "token0": TOKEN0, "token1": TOKEN1})(),  # type: ignore[arg-type]
        lambda _t: None,
    )
    bad_log = {
        "address": POOL.checksum,
        "topics": ["0x" + "0" * 64],
        "data": "0x",
        "blockNumber": "0x1",
        "logIndex": "0x0",
    }
    assert feed._build_tick(bad_log) is None
