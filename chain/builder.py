"""Fluent builder for EIP-1559 transactions (gas, nonce, sign, broadcast)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from eth_account.datastructures import SignedTransaction

from chain.client import ChainClient
from chain.errors import InsufficientFunds, InvalidParameterError, TransactionFailed
from chain.validation import (
    normalize_tx_hash,
    validate_gas_priority,
    validate_timeout_seconds,
)
from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest
from core.wallet import WalletManager

logger = logging.getLogger(__name__)


class TransactionBuilder:
    """Fluent builder for EIP-1559 transactions.

    Typical flow: ``.to()`` → ``.value()`` / ``.data()`` → ``.with_gas_estimate()``
    → ``.with_gas_price()`` → ``.build()`` or ``.send()`` / ``.send_and_wait()``.

    Nonce is filled automatically from :class:`chain.nonce_manager.NonceManager`
    unless :meth:`nonce` is set explicitly.
    """

    def __init__(self, client: ChainClient, wallet: WalletManager) -> None:
        """
        Args:
            client: Connected :class:`ChainClient`.
            wallet: Signer for ``from`` and transaction signing.
        """
        self.client = client
        self.wallet = wallet
        self.w3 = client.w3

        self._tx: Dict[str, Any] = {
            "from": self.wallet.address,
            "chainId": self.w3.eth.chain_id,
            "value": 0,
            "data": b"",
        }

    # ------------------------------------------------------------------
    # Fluent setters
    # ------------------------------------------------------------------

    def to(self, address: Address) -> TransactionBuilder:
        """Set ``to`` (recipient contract or EOA)."""
        self._tx["to"] = address.checksum
        return self

    def value(self, amount: TokenAmount) -> TransactionBuilder:
        """Set native value transfer (wei) from ``amount.raw``."""
        self._tx["value"] = amount.raw
        return self

    def data(self, calldata: bytes) -> TransactionBuilder:
        """Set contract calldata (``input``)."""
        if not isinstance(calldata, (bytes, bytearray, memoryview)):
            raise InvalidParameterError("calldata must be bytes-like.")
        self._tx["data"] = bytes(calldata)
        return self

    def nonce(self, nonce: int) -> TransactionBuilder:
        """Override automatic nonce (replacement txs, explicit sequencing)."""
        if not isinstance(nonce, int) or isinstance(nonce, bool):
            raise InvalidParameterError("nonce must be an integer.")
        if nonce < 0:
            raise InvalidParameterError(f"nonce must be non-negative, got {nonce}.")
        self._tx["nonce"] = nonce
        return self

    def gas_limit(self, limit: int) -> TransactionBuilder:
        """Set gas limit directly (skip estimation)."""
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise InvalidParameterError("gas limit must be an integer.")
        if limit <= 0:
            raise InvalidParameterError(f"gas limit must be positive, got {limit}.")
        self._tx["gas"] = limit
        return self

    def with_gas_estimate(self, buffer: float = 1.2) -> TransactionBuilder:
        """Set ``gas`` via ``eth_estimateGas`` × *buffer*."""
        if isinstance(buffer, bool) or not isinstance(buffer, (int, float)):
            raise InvalidParameterError("buffer must be a number.")
        if buffer <= 0:
            raise InvalidParameterError(f"buffer must be positive, got {buffer}.")
        logger.debug("estimating gas with buffer=%s", buffer)
        estimate = self.client.estimate_gas(self._tx)
        self._tx["gas"] = int(estimate * buffer)
        logger.info("gas set to %s (estimate=%s)", self._tx["gas"], estimate)
        return self

    def with_gas_price(self, priority: str = "medium") -> TransactionBuilder:
        """Set ``maxFeePerGas`` and ``maxPriorityFeePerGas`` from network snapshot."""
        validate_gas_priority(priority)
        gas_price = self.client.get_gas_price()
        self._tx["maxFeePerGas"] = gas_price.get_max_fee(priority)
        self._tx["maxPriorityFeePerGas"] = gas_price.get_priority_fee(priority)
        logger.info(
            "gas price tier=%s maxFee=%s priority=%s",
            priority,
            self._tx["maxFeePerGas"],
            self._tx["maxPriorityFeePerGas"],
        )
        return self

    # ------------------------------------------------------------------
    # Build / sign / send
    # ------------------------------------------------------------------

    def build(self) -> TransactionRequest:
        """Validate required fields and return a :class:`TransactionRequest`."""
        if "nonce" not in self._tx:
            self._tx["nonce"] = self.client.get_nonce(Address.from_string(self.wallet.address))

        missing: list[str] = []
        if "to" not in self._tx:
            missing.append("to  (call .to())")
        if "gas" not in self._tx:
            missing.append("gas  (call .gas_limit() or .with_gas_estimate())")
        if "maxFeePerGas" not in self._tx:
            missing.append("maxFeePerGas  (call .with_gas_price())")
        if "maxPriorityFeePerGas" not in self._tx:
            missing.append("maxPriorityFeePerGas  (call .with_gas_price())")
        if missing:
            raise ValueError("Transaction is incomplete — missing: " + ", ".join(missing))

        return TransactionRequest.from_dict(self._tx)

    def _validate_balance(self, tx: TransactionRequest) -> None:
        """Ensure ETH balance covers ``value`` + ``gas * maxFeePerGas``."""
        balance = self.client.get_balance(Address.from_string(self.wallet.address))
        gas_cost = (tx.gas_limit or 0) * (tx.max_fee_per_gas or 0)
        total = tx.value.raw + gas_cost
        if balance.raw < total:
            logger.warning(
                "insufficient ETH: have=%s need=%s",
                balance.raw,
                total,
            )
            raise InsufficientFunds(
                f"Insufficient ETH: have {balance}, "
                f"need ~{TokenAmount(raw=total, decimals=18, symbol='ETH')} "
                f"(value={tx.value} + maxGasCost="
                f"{TokenAmount(raw=gas_cost, decimals=18, symbol='ETH')})"
            )

    def build_and_sign(self) -> SignedTransaction:
        """``build`` → balance check → sign (``from`` omitted for signing)."""
        tx = self.build()
        self._validate_balance(tx)
        sign_dict = tx.to_dict()
        sign_dict.pop("from", None)
        return self.wallet.sign_transaction(sign_dict)

    def send(self) -> str:
        """Sign and broadcast; return tx hash hex."""
        signed_tx = self.build_and_sign()
        return self.client.send_raw_transaction(signed_tx.raw_transaction)

    def send_and_wait(
        self,
        timeout_seconds: int = 120,
        poll_interval: int = 2,
    ) -> TransactionReceipt:
        """Send and wait for receipt; raise :class:`TransactionFailed` on revert."""
        validate_timeout_seconds("timeout_seconds", timeout_seconds)
        validate_timeout_seconds("poll_interval", poll_interval)
        tx_hash = normalize_tx_hash(self.send())
        receipt = self.client.wait_for_receipt(
            tx_hash,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval,
        )
        if not receipt.status:
            revert_reason = self.client.get_revert_reason(tx_hash)
            logger.error("tx reverted: hash_prefix=%s", tx_hash[:12])
            raise TransactionFailed(tx_hash, receipt, revert_reason)
        return receipt
