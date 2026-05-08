"""Real-time Uniswap V3 pool state via WebSocket ``logs`` (``Swap`` events).

Mirrors :class:`pricing.v2_pool_price_feed.V2PoolPriceFeed` for V3 pools.
``sqrtPriceX96`` from each ``Swap`` log is converted to a human-units spot
price using the pool's ``token0`` / ``token1`` decimals — no local V3 swap
simulation here. Trade-size impact callers should query the live ``QuoterV2``
via :class:`pricing.uniswap_v3_pool.UniswapV3PoolQuoter` instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from eth_utils import to_checksum_address
from web3 import AsyncWeb3
from web3.providers import WebSocketProvider
from web3.types import FormattedEthSubscriptionResponse

from chain.decoder import TransactionDecoder
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter

logger = logging.getLogger(__name__)

# keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)") — V3 pool event.
UNISWAP_V3_SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def _rpc_uint(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return int(val, 16) if val.startswith("0x") else int(val, 10)
    if isinstance(val, (bytes, bytearray, memoryview)):
        return int.from_bytes(val, "big", signed=False)
    return int(val)


def v3_swap_logs_filter(pool_address: str) -> dict:
    """Filter for one V3 pool's ``Swap`` events (use with ``eth_subscribe`` / ``eth_getLogs``)."""
    return {
        "address": to_checksum_address(pool_address),
        "topics": [UNISWAP_V3_SWAP_TOPIC0],
    }


def spot_price_from_sqrt_x96(
    sqrt_price_x96: int,
    *,
    token0_decimals: int,
    token1_decimals: int,
) -> tuple[Decimal, Decimal]:
    """Compute ``(spot_token0_per_token1, spot_token1_per_token0)`` from ``sqrtPriceX96``.

    Uniswap V3 stores the ratio ``sqrt(price) * 2**96``, where ``price`` is
    ``token1 / token0`` in atom units. Conversion to human units folds in the
    decimals delta so the caller gets prices already comparable to a CEX feed.
    """
    if sqrt_price_x96 <= 0:
        return Decimal("0"), Decimal("0")
    q96 = Decimal(2) ** 96
    sqrt_p = Decimal(sqrt_price_x96) / q96
    price_atom = sqrt_p * sqrt_p  # token1_atoms per token0_atom
    decimal_adj = Decimal(10) ** (token0_decimals - token1_decimals)
    price_t1_per_t0 = price_atom * decimal_adj
    if price_t1_per_t0 <= 0:
        return Decimal("0"), Decimal("0")
    price_t0_per_t1 = Decimal(1) / price_t1_per_t0
    return price_t0_per_t1, price_t1_per_t0


@dataclass(frozen=True, slots=True)
class V3PoolPriceTick:
    """One pool state after a ``Swap`` log."""

    block_number: int
    log_index: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    spot_price_token0: Decimal  # token1 per 1 token0 (human units)
    spot_price_token1: Decimal  # token0 per 1 token1 (human units)


class V3PoolPriceFeed:
    """Subscribe to one V3 pool's ``Swap`` logs and emit a tick per update."""

    def __init__(
        self,
        ws_url: str,
        pool: UniswapV3PoolQuoter,
        on_tick: Callable[[V3PoolPriceTick], None],
    ) -> None:
        self._ws_url = ws_url
        self._pool = pool
        self._on_tick = on_tick

    def _build_tick(self, log: Mapping[str, Any]) -> V3PoolPriceTick | None:
        parsed = TransactionDecoder.parse_event(log)
        if parsed.get("name") != "SwapV3":
            return None
        dec = parsed.get("decoded")
        if not isinstance(dec, Mapping):
            return None
        sqrt_x96 = dec.get("sqrtPriceX96")
        liq = dec.get("liquidity")
        tick_v = dec.get("tick")
        if sqrt_x96 is None or liq is None or tick_v is None:
            return None
        sqrt_price_x96 = int(sqrt_x96)
        if sqrt_price_x96 <= 0:
            return None
        # token0_per_token1 = quote per base when token0 is base; we expose both directions.
        s_t1_per_t0_human = spot_price_from_sqrt_x96(
            sqrt_price_x96,
            token0_decimals=self._pool.token0.decimals,
            token1_decimals=self._pool.token1.decimals,
        )
        spot_t0_per_t1, spot_t1_per_t0 = s_t1_per_t0_human
        return V3PoolPriceTick(
            block_number=_rpc_uint(log.get("blockNumber")),
            log_index=_rpc_uint(log.get("logIndex")),
            sqrt_price_x96=sqrt_price_x96,
            tick=int(tick_v),
            liquidity=int(liq),
            spot_price_token0=spot_t1_per_t0,
            spot_price_token1=spot_t0_per_t1,
        )

    def _emit_tick_from_log(self, log_dict: Mapping[str, Any]) -> None:
        tick = self._build_tick(log_dict)
        if tick is not None:
            self._on_tick(tick)

    async def run_forever(self) -> None:
        pool_cs = self._pool.address.checksum
        flt = v3_swap_logs_filter(pool_cs)
        async with AsyncWeb3(WebSocketProvider(self._ws_url)) as w3:
            sub_id = await w3.eth.subscribe("logs", flt)
            try:
                async for msg in w3.socket.process_subscriptions():
                    sub_msg = cast(FormattedEthSubscriptionResponse, msg)
                    if str(sub_msg.get("subscription")) != str(sub_id):
                        continue
                    result = sub_msg.get("result")
                    if not isinstance(result, Mapping):
                        continue
                    log_dict = dict(result)
                    await asyncio.to_thread(self._emit_tick_from_log, log_dict)
            finally:
                try:
                    await w3.eth.unsubscribe(sub_id)
                except Exception as err:
                    logger.debug("unsubscribe failed: %s", err)
