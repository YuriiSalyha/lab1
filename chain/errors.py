"""Typed exceptions for JSON-RPC, transactions, gas, and node health.

Use these instead of raw strings so callers can branch on error kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.types import TransactionReceipt


class ChainError(Exception):
    """Base class for all chain-layer errors."""

    pass


class InvalidParameterError(ChainError):
    """Caller passed an invalid argument (type, range, or format)."""

    pass


class RPCError(ChainError):
    """JSON-RPC call failed or returned an unexpected result."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        self.code = code
        super().__init__(message)


class TransactionFailed(ChainError):
    """Transaction was included but reverted (``status == 0``)."""

    def __init__(
        self,
        tx_hash: str,
        receipt: TransactionReceipt,
        revert_reason: Optional[str] = None,
    ) -> None:
        self.tx_hash = tx_hash
        self.receipt = receipt
        self.revert_reason = revert_reason
        msg = f"Transaction {tx_hash} reverted"
        if revert_reason:
            msg += f": {revert_reason}"
        super().__init__(msg)


class InsufficientFunds(ChainError):
    """Sender ETH balance is below value + max gas cost."""

    pass


class InsufficientTokenBalance(ChainError):
    """ERC-20 balance too low for the intended transfer (caller-specific)."""

    pass


class NonceTooLow(ChainError):
    """Submitted nonce was already used on-chain."""

    pass


class NonceTooHigh(ChainError):
    """Nonce skips ahead of the next expected value (gap)."""

    pass


class ReplacementUnderpriced(ChainError):
    """Same-nonce replacement tx carried too low a fee."""

    pass


class TransactionTimeout(ChainError):
    """Receipt not observed within the polling deadline."""

    def __init__(self, tx_hash: str, timeout_seconds: int) -> None:
        self.tx_hash = tx_hash
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Transaction {tx_hash} not confirmed within {timeout_seconds}s")


class NodeLagging(ChainError):
    """RPC ``latest`` block looks stale compared to wall clock."""

    def __init__(
        self,
        message: str,
        local_block: Optional[int] = None,
        expected_block: Optional[int] = None,
    ) -> None:
        self.local_block = local_block
        self.expected_block = expected_block
        super().__init__(message)


class GasEstimationFailed(ChainError):
    """``eth_estimateGas`` or simulation indicated the tx would revert."""

    def __init__(self, message: str, revert_reason: Optional[str] = None) -> None:
        self.revert_reason = revert_reason
        super().__init__(message)
