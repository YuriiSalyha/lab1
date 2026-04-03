"""Ethereum JSON-RPC client: retries, typed errors, gas, receipts, decoding helpers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted

from chain.decoder import TransactionDecoder
from chain.errors import (
    ChainError,
    GasEstimationFailed,
    InsufficientFunds,
    NodeLagging,
    NonceTooHigh,
    NonceTooLow,
    ReplacementUnderpriced,
    RPCError,
    TransactionTimeout,
)
from chain.nonce_manager import NonceManager
from chain.validation import (
    normalize_tx_hash,
    require_bytes,
    validate_block_identifier,
    validate_buffer_bps,
    validate_gas_priority,
    validate_max_retries,
    validate_rpc_urls,
    validate_timeout_seconds,
    validate_token_address_str,
    validate_tx_dict,
)
from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest

logger = logging.getLogger(__name__)


def _format_contract_logic_revert(err: ContractLogicError) -> str:
    """Format ``ContractLogicError`` for display (ABI-decode ``data`` when possible)."""
    msg = err.message
    if isinstance(msg, str):
        msg = msg.strip() or None
    else:
        msg = None
    data = err.data

    decoded: Optional[str] = None
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        decoded = TransactionDecoder.decode_revert_reason(data)
    elif isinstance(data, (bytes, bytearray, memoryview)):
        decoded = TransactionDecoder.decode_revert_reason(bytes(data))

    if decoded:
        if msg and decoded in msg:
            return msg
        return decoded
    return msg or str(err)


# Minimal ABI fragments for lazy token metadata lookups
_ERC20_METADATA_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"type": "string"}],
        "type": "function",
    },
]

# If latest block is older than this many seconds, treat the node as lagging
_NODE_LAG_THRESHOLD_SECONDS = 120


def _receipt_from_web3(raw: Any) -> TransactionReceipt:
    """Normalize a Web3 receipt object to our ``TransactionReceipt``."""
    return TransactionReceipt.from_web3(raw)


class TokenMetadataCache:
    """Lazy cache of ERC-20 ``symbol``, ``decimals``, and ``name`` per contract address."""

    def __init__(self, w3: Web3) -> None:
        self._w3 = w3
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, token_address: str) -> dict[str, Any]:
        """Return metadata dict for *token_address* (fetch once, then cache).

        Args:
            token_address: Token contract address string.

        Returns:
            Dict with ``address``, ``symbol``, ``decimals``, ``name``
            (placeholders if RPC calls fail).
        """
        validate_token_address_str(token_address)
        key = token_address.lower()
        if key in self._cache:
            logger.debug("token cache hit: suffix=%s", key[-8:])
            return self._cache[key]

        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=_ERC20_METADATA_ABI,
        )
        info: dict[str, Any] = {"address": token_address}
        for field in ("symbol", "decimals", "name"):
            try:
                info[field] = getattr(contract.functions, field)().call()
            except Exception as err:
                logger.debug("token field %s failed for %s: %s", field, key[-8:], err)
                info[field] = {"symbol": "UNKNOWN", "decimals": 18, "name": "Unknown"}[field]

        self._cache[key] = info
        logger.info("fetched token metadata: suffix=%s symbol=%s", key[-8:], info.get("symbol"))
        return info

    def invalidate(self, token_address: str) -> None:
        """Drop one entry from the cache."""
        validate_token_address_str(token_address)
        self._cache.pop(token_address.lower(), None)


class ChainClient:
    """JSON-RPC client with multi-endpoint retry, error typing, and decode helpers."""

    def __init__(
        self,
        rpc_urls: list[str],
        timeout_seconds: int = 30,
        max_retries: int = 3,
    ) -> None:
        validate_rpc_urls(rpc_urls)
        validate_timeout_seconds("timeout_seconds", timeout_seconds)
        validate_max_retries(max_retries)

        self.rpc_urls = [u.strip() for u in rpc_urls]
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

        self._web3_instances = [
            Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout_seconds}))
            for url in self.rpc_urls
        ]
        self.w3: Web3 = self._web3_instances[0]
        logger.info("ChainClient init: %s endpoint(s), timeout=%ss", len(rpc_urls), timeout_seconds)

        self._nonce_managers: dict[str, NonceManager] = {}
        self.token_cache = TokenMetadataCache(self.w3)

    # ------------------------------------------------------------------
    # Retry / error helpers
    # ------------------------------------------------------------------

    def _execute_with_retry(self, func_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``w3.eth.<func_name>`` across endpoints with backoff."""
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            for idx, w3 in enumerate(self._web3_instances):
                try:
                    attr = getattr(w3.eth, func_name)
                    if callable(attr):
                        out = attr(*args, **kwargs)
                    else:
                        out = attr
                    logger.debug("eth.%s ok via endpoint_index=%s", func_name, idx)
                    return out
                except Exception as e:
                    last_error = e
                    non_retryable = self._classify_error(e)
                    if non_retryable is not None:
                        logger.warning("non-retryable RPC error on %s: %s", func_name, e)
                        raise non_retryable from e
                    logger.debug("eth.%s failed on endpoint %s: %s", func_name, idx, e)
                    continue

            if attempt < self.max_retries - 1:
                delay = 2**attempt
                logger.info(
                    "RPC retry cycle %s/%s, sleeping %ss",
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)

        logger.error("eth.%s exhausted retries", func_name)
        raise RPCError(
            f"Operation '{func_name}' failed after {self.max_retries} retries: {last_error}"
        )

    @staticmethod
    def _classify_error(error: Exception) -> Optional[ChainError]:
        """Map common RPC strings to typed exceptions; ``None`` = retryable."""
        msg = str(error).lower()

        if "insufficient funds" in msg or "insufficient balance" in msg:
            return InsufficientFunds(str(error))
        if "nonce too low" in msg or "already known" in msg:
            return NonceTooLow(str(error))
        if "nonce too high" in msg:
            return NonceTooHigh(str(error))
        if "replacement transaction underpriced" in msg:
            return ReplacementUnderpriced(str(error))
        if "execution reverted" in msg:
            return GasEstimationFailed(str(error), revert_reason=str(error))

        return None

    # ------------------------------------------------------------------
    # Core RPC wrappers
    # ------------------------------------------------------------------

    def get_balance(self, address: Address) -> TokenAmount:
        """Return ETH balance for *address* as ``TokenAmount`` (18 decimals)."""
        raw = self._execute_with_retry("get_balance", address.checksum)
        return TokenAmount(raw=raw, decimals=18, symbol="ETH")

    def get_nonce(self, address: Address) -> int:
        """Next nonce for *address* (thread-safe local counter + chain ``pending``)."""
        key = address.checksum
        if key not in self._nonce_managers:
            self._nonce_managers[key] = NonceManager(key, self.w3)
        return self._nonce_managers[key].get_nonce()

    def estimate_gas(self, tx: dict) -> int:
        """Return ``eth_estimateGas`` for *tx* dict."""
        validate_tx_dict(tx)
        try:
            return self._execute_with_retry("estimate_gas", tx)
        except ContractLogicError as e:
            raise GasEstimationFailed(
                f"Transaction would revert: {e}",
                revert_reason=str(e),
            ) from e
        except GasEstimationFailed:
            raise
        except ChainError:
            raise
        except Exception as e:
            raise GasEstimationFailed(f"Gas estimation failed: {e}") from e

    def get_gas_price(self) -> GasPrice:
        """Latest block base fee + suggested priority fee tiers."""
        latest_block = self._execute_with_retry("get_block", "latest")
        base_fee: int = latest_block.get("baseFeePerGas", 0)

        max_priority_fee: int = self._execute_with_retry("max_priority_fee")

        return GasPrice(
            base_fee=base_fee,
            priority_fee_low=int(max_priority_fee * 0.8),
            priority_fee_medium=max_priority_fee,
            priority_fee_high=int(max_priority_fee * 1.5),
        )

    # ------------------------------------------------------------------
    # Sending & receipts
    # ------------------------------------------------------------------

    def send_raw_transaction(self, raw_tx: bytes) -> str:
        """Broadcast raw signed tx bytes; return tx hash hex string."""
        require_bytes("raw_tx", raw_tx, allow_empty=False)
        raw_tx = bytes(raw_tx)
        tx_hash = self._execute_with_retry("send_raw_transaction", raw_tx)
        h = tx_hash.hex()
        logger.info("submitted raw tx, hash_prefix=%s", h[:12])
        return h

    def wait_for_receipt(
        self,
        tx_hash: str,
        timeout_seconds: int = 120,
        poll_interval_seconds: int = 2,
    ) -> TransactionReceipt:
        """Poll until receipt or timeout."""
        tx_hash = normalize_tx_hash(tx_hash)
        validate_timeout_seconds("timeout_seconds", timeout_seconds)
        validate_timeout_seconds("poll_interval_seconds", poll_interval_seconds)
        try:
            raw_receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=timeout_seconds,
                poll_latency=poll_interval_seconds,
            )
            rec = _receipt_from_web3(raw_receipt)
            logger.info(
                "receipt received: block=%s status=%s",
                rec.block_number,
                rec.status,
            )
            return rec
        except TimeExhausted as err:
            logger.error("receipt timeout: hash_prefix=%s", tx_hash[:12])
            raise TransactionTimeout(tx_hash, timeout_seconds) from err

    def get_transaction(self, tx_hash: str) -> dict:
        """Fetch transaction by hash; raises ``RPCError`` if missing or RPC fails."""
        tx_hash = normalize_tx_hash(tx_hash)
        try:
            tx = self._execute_with_retry("get_transaction", tx_hash)
            if tx is None:
                raise RPCError(f"Transaction not found: {tx_hash}")
            return dict(tx)
        except ChainError:
            raise
        except Exception as e:
            raise RPCError(f"Failed to fetch transaction {tx_hash}: {e}") from e

    def get_receipt(self, tx_hash: str) -> Optional[TransactionReceipt]:
        """Receipt if mined, else ``None`` (pending or unknown)."""
        tx_hash = normalize_tx_hash(tx_hash)
        try:
            raw = self.w3.eth.get_transaction_receipt(tx_hash)
            if raw is None:
                return None
            return _receipt_from_web3(raw)
        except Exception as err:
            logger.debug("get_receipt failed: %s", err)
            return None

    def call(self, tx: dict | TransactionRequest, block: str = "latest") -> bytes:
        """``eth_call`` — pass a dict or ``TransactionRequest``."""
        validate_block_identifier(block)
        if isinstance(tx, TransactionRequest):
            tx = tx.to_dict()
        else:
            validate_tx_dict(tx)
        return self._execute_with_retry("call", tx, block)

    # ------------------------------------------------------------------
    # Revert reason
    # ------------------------------------------------------------------

    def get_revert_reason(self, tx_hash: str) -> Optional[str]:
        """Replay failed tx at inclusion block to surface revert data (best-effort)."""
        tx_hash = normalize_tx_hash(tx_hash)
        try:
            tx = self.get_transaction(tx_hash)
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None or receipt["status"] == 1:
                return None

            self.w3.eth.call(
                {
                    "from": tx["from"],
                    "to": tx["to"],
                    "value": tx.get("value", 0),
                    "data": tx.get("input", b""),
                    "gas": tx["gas"],
                },
                receipt["blockNumber"],
            )
            return None
        except ContractLogicError as e:
            return _format_contract_logic_revert(e)
        except Exception as e:
            text = TransactionDecoder.humanize_revert_tuple_string(str(e))
            if text.startswith("0x") and len(text) >= 10:
                abi_text = TransactionDecoder.decode_revert_reason(text)
                if abi_text:
                    return abi_text
            return text

    # ------------------------------------------------------------------
    # Node health
    # ------------------------------------------------------------------

    def check_node_health(self) -> None:
        """Raise ``NodeLagging`` if the node's ``latest`` block timestamp is too old."""
        try:
            block = self.w3.eth.get_block("latest")
        except Exception as e:
            raise RPCError(f"Cannot reach node: {e}") from e

        block_age = int(time.time()) - block["timestamp"]
        if block_age > _NODE_LAG_THRESHOLD_SECONDS:
            logger.warning("node lag: block=%s age_seconds=%s", block["number"], block_age)
            raise NodeLagging(
                f"Node is {block_age}s behind (threshold: {_NODE_LAG_THRESHOLD_SECONDS}s)",
                local_block=block["number"],
            )

    # ------------------------------------------------------------------
    # Decoding helpers
    # ------------------------------------------------------------------

    def decode_transaction(self, tx_hash: str) -> dict[str, Any]:
        """Load tx by hash and attach decoded calldata under decoder output + ``tx`` key."""
        tx_hash = normalize_tx_hash(tx_hash)
        tx = self.get_transaction(tx_hash)
        calldata = tx.get("input", b"")
        decoded = TransactionDecoder.decode_function_call(calldata)
        decoded["tx"] = tx
        return decoded

    def parse_receipt_events(self, tx_hash: str) -> list[dict[str, Any]]:
        """Parse logs from mined receipt; empty list if still pending."""
        tx_hash = normalize_tx_hash(tx_hash)
        receipt = self.get_receipt(tx_hash)
        if receipt is None:
            return []
        return TransactionDecoder.parse_events(receipt.logs)

    def get_tx_status(self, tx_hash: str) -> str:
        """``success`` | ``reverted`` | ``pending`` | ``unknown``."""
        tx_hash = normalize_tx_hash(tx_hash)
        try:
            receipt = self.get_receipt(tx_hash)
            if receipt is None:
                return "pending"
            return "success" if receipt.status else "reverted"
        except Exception:
            return "unknown"


# ======================================================================
# GasPrice value object
# ======================================================================


@dataclass
class GasPrice:
    """EIP-1559 fee snapshot: base fee plus low/medium/high priority fee suggestions."""

    base_fee: int
    priority_fee_low: int
    priority_fee_medium: int
    priority_fee_high: int

    def get_priority_fee(self, priority: str = "medium") -> int:
        """Tip component for *priority* tier (``low`` / ``medium`` / ``high``)."""
        validate_gas_priority(priority)
        if priority == "low":
            return self.priority_fee_low
        if priority == "high":
            return self.priority_fee_high
        return self.priority_fee_medium

    def get_max_fee(self, priority: str = "medium", buffer_bps: int = 2000) -> int:
        """``maxFeePerGas`` ≈ buffered base + tip; *buffer_bps* in basis points (2000 = 20%)."""
        validate_gas_priority(priority)
        validate_buffer_bps(buffer_bps)
        tip = self.get_priority_fee(priority)
        buffered_base = self.base_fee * (10_000 + buffer_bps) // 10_000
        return buffered_base + tip
