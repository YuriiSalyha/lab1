"""ERC-20 router-allowance helper.

Live Uniswap V2/V3 swaps require the router to be approved as a spender of the
input ERC-20. Without it, every swap reverts inside ``TransferHelper.safeTransferFrom``
with the canonical ``"TransferHelper: TRANSFER_FROM_FAILED"`` message — which is
exactly what we saw in production when the bot first crossed the wallet's USDT
into a real DEX leg.

This module is a single-purpose, idempotent helper:

* :func:`ensure_router_allowance` reads ``allowance(owner, spender)`` and, when
  it is below ``min_amount``, broadcasts an ``approve(spender, max_uint256)``
  transaction and waits for it to mine before returning. Subsequent calls in
  the same process are short-circuited via :data:`_APPROVED_CACHE` so each
  ``(token, spender)`` pair triggers at most one on-chain approval per run.

The approval is set to ``2**256 - 1`` so we only ever pay for the approve once
per token; this is the same strategy used by every Uniswap front-end and by the
fork-swap test fixture in ``tests/test_fork_swap_executor.py``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from eth_abi import encode as abi_encode
from web3 import Web3

from chain.builder import TransactionBuilder
from chain.client import ChainClient
from core.types import Address
from core.wallet import WalletManager

logger = logging.getLogger(__name__)

# 4-byte selectors (keccak256 of canonical signature, big-endian).
_APPROVE_SELECTOR = bytes.fromhex("095ea7b3")  # approve(address,uint256)
_ALLOWANCE_SELECTOR = bytes.fromhex("dd62ed3e")  # allowance(address,address)
MAX_UINT256 = (1 << 256) - 1

_ALLOWANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function",
    },
]

# Per-process memo: ``(token_lower, spender_lower) -> True`` once the router has
# been approved for ``MAX_UINT256``. Thread-safe so concurrent ticks (Telegram
# control loop, etc.) don't race on the same approval. Cleared automatically
# when the process exits, which matches the lifetime of an unbounded approval.
_APPROVED_CACHE: dict[tuple[str, str], bool] = {}
_CACHE_LOCK = threading.Lock()


def _encode_approve(spender: Address, amount: int) -> bytes:
    """Encode ``approve(spender, amount)`` calldata."""
    if amount < 0 or amount > MAX_UINT256:
        raise ValueError(f"approve amount out of uint256 range: {amount}")
    return _APPROVE_SELECTOR + abi_encode(
        ["address", "uint256"],
        [spender.checksum, amount],
    )


def get_allowance(client: ChainClient, token: Address, owner: Address, spender: Address) -> int:
    """Return raw ``allowance(owner, spender)`` for ``token`` (uint256)."""
    contract = client.w3.eth.contract(
        address=Web3.to_checksum_address(token.checksum),
        abi=_ALLOWANCE_ABI,
    )
    raw = contract.functions.allowance(
        Web3.to_checksum_address(owner.checksum),
        Web3.to_checksum_address(spender.checksum),
    ).call()
    return int(raw)


def ensure_router_allowance(
    *,
    client: ChainClient,
    wallet: WalletManager,
    token: Address,
    spender: Address,
    min_amount: int,
    receipt_timeout_s: int = 120,
) -> dict[str, Any]:
    """Idempotently ensure ``allowance(wallet, spender) >= min_amount`` for ``token``.

    Returns a dict with the chosen path:

    * ``{"approved": False, "reason": "cached"}`` — already done in this process.
    * ``{"approved": False, "reason": "sufficient", "current": int}`` — chain
      already has enough allowance (e.g. set by a previous run / front-end).
    * ``{"approved": True, "tx_hash": str, "current": MAX_UINT256}`` — we just
      sent (and mined) an ``approve(spender, max_uint256)`` transaction.

    The ``min_amount`` check is exact: we approve **only when the on-chain
    allowance is strictly below it**. Combined with the ``MAX_UINT256`` topup,
    this means a token gets approved at most once across the lifetime of the
    bot's wallet.
    """
    if min_amount <= 0:
        return {"approved": False, "reason": "zero_min", "current": 0}

    cache_key = (token.lower, spender.lower)
    with _CACHE_LOCK:
        if cache_key in _APPROVED_CACHE:
            return {"approved": False, "reason": "cached"}

    owner = Address.from_string(wallet.address)
    current = get_allowance(client, token, owner, spender)
    if current >= min_amount:
        with _CACHE_LOCK:
            _APPROVED_CACHE[cache_key] = True
        logger.info(
            "router allowance already sufficient token_suffix=%s spender_suffix=%s current=%s",
            token.lower[-8:],
            spender.lower[-8:],
            current,
        )
        return {"approved": False, "reason": "sufficient", "current": current}

    logger.info(
        "approving router as ERC20 spender token_suffix=%s spender_suffix=%s current=%s",
        token.lower[-8:],
        spender.lower[-8:],
        current,
    )
    calldata = _encode_approve(spender, MAX_UINT256)
    builder = TransactionBuilder(client, wallet)
    builder.to(token)
    builder.data(calldata)
    builder.with_gas_estimate()
    builder.with_gas_price()
    receipt = builder.send_and_wait(timeout_seconds=receipt_timeout_s)
    tx_hash = getattr(receipt, "tx_hash", "") or ""
    with _CACHE_LOCK:
        _APPROVED_CACHE[cache_key] = True
    logger.info(
        "router allowance set tx=%s token_suffix=%s spender_suffix=%s",
        str(tx_hash)[:18],
        token.lower[-8:],
        spender.lower[-8:],
    )
    return {"approved": True, "tx_hash": str(tx_hash), "current": MAX_UINT256}


def reset_approved_cache_for_tests() -> None:
    """Clear the in-process allowance memo (test hook only)."""
    with _CACHE_LOCK:
        _APPROVED_CACHE.clear()
