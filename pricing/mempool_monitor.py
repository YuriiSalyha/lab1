"""Async mempool monitor: WebSocket pending txs + Uniswap V2 swap decoding."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional, cast

from web3 import AsyncWeb3
from web3.providers import WebSocketProvider
from web3.types import FormattedEthSubscriptionResponse

from chain.decoder import TransactionDecoder
from pricing.parsed_swap import ParsedSwap, try_parse_uniswap_v2_swap

logger = logging.getLogger(__name__)


def _calldata_bytes(raw: Any) -> Optional[bytes]:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    return None


class MempoolMonitor:
    """Subscribe to pending transactions over WebSocket and surface Uniswap V2 swaps."""

    def __init__(
        self,
        ws_url: str,
        callback: Callable[[ParsedSwap], None],
        *,
        full_transactions: bool = True,
        max_concurrent: int = 32,
    ) -> None:
        self.ws_url = ws_url
        self.callback = callback
        self._full_transactions = full_transactions
        self._sem = asyncio.Semaphore(max_concurrent)

    def parse_transaction(self, tx: Mapping[str, Any]) -> Optional[ParsedSwap]:
        """Decode calldata with ``TransactionDecoder``; map V2 swaps to ``ParsedSwap``."""

        tx_dict = dict(tx)
        raw_input = tx_dict.get("input")
        if raw_input is None:
            raw_input = tx_dict.get("data")
        data = _calldata_bytes(raw_input)
        if not data or len(data) < 4:
            return None

        decoded = TransactionDecoder.decode_function_call(data)
        return try_parse_uniswap_v2_swap(tx_dict, decoded)

    async def start(self) -> None:
        """WS connect, ``newPendingTransactions`` subscription, *callback* for each V2 swap."""

        async with AsyncWeb3(WebSocketProvider(self.ws_url)) as w3:
            sub_id = await w3.eth.subscribe("newPendingTransactions", self._full_transactions)
            try:
                async for msg in w3.socket.process_subscriptions():
                    sub_msg = cast(FormattedEthSubscriptionResponse, msg)
                    await self._on_subscription_message(w3, sub_msg, sub_id)
            finally:
                try:
                    await w3.eth.unsubscribe(sub_id)
                except Exception as err:
                    logger.debug("unsubscribe failed: %s", err)

    async def _on_subscription_message(
        self,
        w3: AsyncWeb3,
        msg: FormattedEthSubscriptionResponse,
        sub_id: str,
    ) -> None:
        msg_sub = msg.get("subscription")
        if str(msg_sub) != str(sub_id):
            return

        result = msg.get("result")
        asyncio.create_task(self._handle_pending_result(w3, result))

    async def _handle_pending_result(self, w3: AsyncWeb3, result: Any) -> None:
        tx: Optional[dict[str, Any]] = None
        try:
            if isinstance(result, str) and result.startswith("0x") and len(result) == 66:
                hx = result
                try:
                    raw = await w3.eth.get_transaction(hx)
                except Exception as err:
                    logger.debug("get_transaction failed for %s: %s", hx[:18], err)
                    return
                if raw is None:
                    return
                tx = dict(raw)
            elif isinstance(result, (bytes, bytearray, memoryview)):
                hx = result.hex() if hasattr(result, "hex") else bytes(result).hex()
                if not hx.startswith("0x"):
                    hx = "0x" + hx
                try:
                    raw = await w3.eth.get_transaction(hx)
                except Exception as err:
                    logger.debug("get_transaction failed for %s: %s", hx[:18], err)
                    return
                if raw is None:
                    return
                tx = dict(raw)
            elif isinstance(result, Mapping):
                tx = dict(result)
            else:
                return
        except Exception as err:
            logger.debug("resolve pending tx failed: %s", err)
            return

        assert tx is not None
        tx_final = tx

        async def _work() -> None:
            async with self._sem:

                def _parse_and_notify() -> None:
                    swap = self.parse_transaction(tx_final)
                    if swap is not None:
                        self.callback(swap)

                await asyncio.to_thread(_parse_and_notify)

        asyncio.create_task(_work())
