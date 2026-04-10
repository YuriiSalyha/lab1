"""Uniswap V2 router swap on a local fork: preflight call, sign, broadcast, parse logs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from chain.builder import TransactionBuilder
from chain.client import ChainClient
from chain.uniswap_v2_router import encode_uniswap_v2_swap_calldata
from core.types import Address, TokenAmount, TransactionReceipt
from core.wallet import WalletManager
from pricing.fork_simulator import ForkSimulator, SimulationResult
from pricing.route import Route

logger = logging.getLogger(__name__)


class ForkSwapError(Exception):
    """Raised when preflight simulation fails or swap execution cannot proceed."""


@dataclass
class ForkSwapExecutionResult:
    """Outcome of :func:`execute_swap_exact_tokens_for_tokens_on_fork`."""

    tx_hash: str
    receipt: TransactionReceipt
    parsed_events: list[dict[str, Any]]
    preflight: Optional[SimulationResult]


def execute_swap_exact_tokens_for_tokens_on_fork(
    fork_client: ChainClient,
    wallet: WalletManager,
    router: Address,
    route: Route,
    amount_in: int,
    amount_out_min: int,
    deadline: int,
    *,
    run_preflight: bool = True,
    recipient: Optional[Address] = None,
) -> ForkSwapExecutionResult:
    """
    On-fork ``swapExactTokensForTokens`` (one router call; multi-hop via ``path``).

    1. Optional preflight: :meth:`ForkSimulator.simulate_route` (``eth_call``).
    2. Build the same calldata as simulation via :func:`encode_uniswap_v2_swap_calldata`.
    3. Sign and broadcast with :class:`~chain.builder.TransactionBuilder` (EIP-1559 gas).
    4. Parse receipt logs with :meth:`ChainClient.parse_receipt_events`.

    **Caller responsibilities:** ``fork_client`` must point at the fork RPC (correct ``chainId``).
    The signing account needs ETH for gas and **ERC-20 allowance** on ``router`` for ``token_in``.
    This module does not wrap ETH, approve the router, or start Anvil.
    """
    if amount_in <= 0:
        raise ForkSwapError(f"amount_in must be positive, got {amount_in}")

    sender = Address.from_string(wallet.address)
    to_addr = recipient if recipient is not None else sender
    path_tokens = [t.address for t in route.path]

    preflight: Optional[SimulationResult] = None
    if run_preflight:
        fork_url = fork_client.rpc_urls[0]
        sim = ForkSimulator(fork_url).simulate_route(
            router,
            route,
            amount_in,
            sender,
            deadline,
            recipient=recipient,
            amount_out_min=amount_out_min,
        )
        preflight = sim
        if not sim.success:
            raise ForkSwapError(f"Preflight simulation failed: {sim.error}")

    calldata = encode_uniswap_v2_swap_calldata(
        "swapExactTokensForTokens",
        path=path_tokens,
        to=to_addr,
        deadline=deadline,
        amount_in=amount_in,
        amount_out_min=amount_out_min,
    )

    builder = TransactionBuilder(fork_client, wallet)
    builder.to(router)
    builder.data(calldata)
    builder.value(TokenAmount(raw=0, decimals=18))
    builder.with_gas_estimate()
    builder.with_gas_price()

    receipt = builder.send_and_wait()
    tx_hash = receipt.tx_hash
    parsed = fork_client.parse_receipt_events(tx_hash)
    logger.info(
        "fork swap mined: hash_prefix=%s events=%s",
        tx_hash[:12],
        len(parsed),
    )
    return ForkSwapExecutionResult(
        tx_hash=tx_hash,
        receipt=receipt,
        parsed_events=parsed,
        preflight=preflight,
    )
