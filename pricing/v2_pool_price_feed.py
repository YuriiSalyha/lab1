"""Real-time Uniswap V2 pool reserves via WebSocket ``logs`` subscription (``Sync`` events)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from web3 import AsyncWeb3
from web3.providers import WebSocketProvider
from web3.types import FormattedEthSubscriptionResponse

from chain.decoder import TransactionDecoder
from chain.uniswap_v2_events import reserves_from_sync_parse_result, sync_logs_filter
from core.types import Token
from pricing.price_impact_analyzer import impact_row_for_amount
from pricing.uniswap_v2_pair import UniswapV2Pair

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class V2PoolPriceTick:
    """One pool state after a ``Sync`` (spot = out per 1 unit token in)."""

    block_number: int
    log_index: int
    reserve0: int
    reserve1: int
    spot_price_token0: Decimal
    spot_price_token1: Decimal
    impact_pct_by_amount: dict[int, Decimal] | None = None


class V2PoolPriceFeed:
    """
    Subscribe to ``Sync`` logs for one V2 pair; invoke *on_tick* for each valid update.
    """

    def __init__(
        self,
        ws_url: str,
        template_pair: UniswapV2Pair,
        on_tick: Callable[[V2PoolPriceTick], None],
        *,
        impact_token: Token | None = None,
        impact_amounts: list[int] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._template = template_pair
        self._on_tick = on_tick
        self._impact_token = impact_token
        self._impact_amounts = list(impact_amounts) if impact_amounts else []

    def _build_tick(self, log: Mapping[str, Any]) -> V2PoolPriceTick | None:
        parsed = TransactionDecoder.parse_event(log)
        got = reserves_from_sync_parse_result(parsed)
        if got is None:
            return None
        r0, r1 = got
        pair = self._template.with_reserves(r0, r1)
        spot0 = pair.get_spot_price(pair.token0)
        spot1 = pair.get_spot_price(pair.token1)
        block_number = _rpc_uint(log.get("blockNumber"))
        log_index = _rpc_uint(log.get("logIndex"))
        impacts: dict[int, Decimal] | None = None
        if self._impact_token is not None and self._impact_amounts:
            impacts = {}
            for amt in self._impact_amounts:
                if amt <= 0:
                    continue
                row = impact_row_for_amount(pair, self._impact_token, amt)
                impacts[amt] = cast(Decimal, row["price_impact_pct"])
        return V2PoolPriceTick(
            block_number=block_number,
            log_index=log_index,
            reserve0=r0,
            reserve1=r1,
            spot_price_token0=spot0,
            spot_price_token1=spot1,
            impact_pct_by_amount=impacts,
        )

    def _emit_tick_from_log(self, log_dict: Mapping[str, Any]) -> None:
        tick = self._build_tick(log_dict)
        if tick is not None:
            self._on_tick(tick)

    async def run_forever(self) -> None:
        pair_cs = self._template.address.checksum
        flt = sync_logs_filter(pair_cs)
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
