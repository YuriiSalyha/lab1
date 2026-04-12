"""Batch quote execution via Multicall3 (one RPC for many Quoter / pool calls)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from web3 import Web3

from chain.multicall import MulticallCall, MulticallResult, aggregate3
from pricing.liquidity_pool import QuoteResult


@dataclass(frozen=True, slots=True)
class RawCallQuoteRequest:
    """One static call to decode elsewhere (e.g. QuoterV2 return ABI)."""

    target: str
    data: bytes
    allow_failure: bool = True


class BatchQuoteExecutor:
    """
    Runs many ``eth_call``-equivalent operations in a single ``aggregate3`` RPC.

    Inject ``w3_factory`` for tests (returns Web3).
    """

    def __init__(
        self,
        w3: Web3 | None = None,
        *,
        w3_factory: Callable[[], Web3] | None = None,
    ) -> None:
        if (w3 is None) == (w3_factory is None):
            raise ValueError("Provide exactly one of w3 or w3_factory")
        self._w3 = w3
        self._w3_factory = w3_factory

    def _web3(self) -> Web3:
        if self._w3 is not None:
            return self._w3
        assert self._w3_factory is not None
        return self._w3_factory()

    def execute_raw(
        self,
        requests: list[RawCallQuoteRequest],
        *,
        block_identifier: Any = "latest",
    ) -> list[MulticallResult]:
        calls = [
            MulticallCall(target=r.target, data=r.data, allow_failure=r.allow_failure)
            for r in requests
        ]
        return aggregate3(self._web3(), calls, block_identifier=block_identifier)

    def execute_quote_results(
        self,
        requests: list[RawCallQuoteRequest],
        decode: Callable[[bytes], QuoteResult],
        *,
        block_identifier: Any = "latest",
    ) -> list[QuoteResult | BaseException]:
        """
        Run batch RPC; map each successful return payload through ``decode``.

        Failed inner calls become :class:`ValueError` entries (or re-raise from decode).
        """
        results = self.execute_raw(requests, block_identifier=block_identifier)
        out: list[QuoteResult | BaseException] = []
        for r in results:
            if not r.success:
                out.append(ValueError("multicall inner call reverted"))
                continue
            try:
                out.append(decode(r.return_data))
            except BaseException as e:
                out.append(e)
        return out
