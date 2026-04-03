"""Domain types: Ethereum addresses, token amounts, tx requests, and receipts."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Optional

from eth_utils import is_address, to_checksum_address

from core.errors import InvalidAddressError, TokenMathError, WalletValidationError

logger = logging.getLogger(__name__)

# 78 digits covers uint256 (2^256 ≈ 1.16e77) with room to spare.
_DECIMAL_PRECISION = 78


@contextmanager
def _high_precision():
    """Temporarily raise Decimal precision for uint256-scale math."""
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        yield ctx


def _parse_to_address(to_val: Any) -> Address:
    """Coerce *to_val* to :class:`Address` (used by :meth:`TransactionRequest.from_dict`)."""
    if isinstance(to_val, Address):
        return to_val
    if isinstance(to_val, str):
        return Address.from_string(to_val)
    raise ValueError("Transaction must have a valid 'to' address")


def _parse_value_field(value_val: Any) -> TokenAmount:
    """Coerce *value* field to :class:`TokenAmount` (wei, 18 decimals)."""
    if isinstance(value_val, TokenAmount):
        return value_val
    return TokenAmount(raw=int(value_val), decimals=18)


def _receipt_tx_hash_hex(receipt: dict[str, Any]) -> str:
    """Normalize ``transactionHash`` from a Web3 receipt to a ``0x`` hex string."""
    th = receipt["transactionHash"]
    return th.hex() if hasattr(th, "hex") else str(th)


@dataclass(frozen=True)
class Address:
    """EIP-55 checksummed Ethereum address (20 bytes, ``0x`` + 40 hex)."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise WalletValidationError("Address must be a string")
        if not self.value.startswith("0x"):
            raise InvalidAddressError(f"Address must start with '0x': {self.value}")
        if not is_address(self.value):
            raise InvalidAddressError(f"Invalid Ethereum address: {self.value}")
        object.__setattr__(self, "value", to_checksum_address(self.value))

    @classmethod
    def from_string(cls, s: str) -> Address:
        """
        Args:
            s: Hex address string (any casing; stored checksummed).

        Returns:
            Validated :class:`Address`.
        """
        return cls(value=s)

    @property
    def checksum(self) -> str:
        """Checksummed ``0x`` form (same as ``value``)."""
        return self.value

    @property
    def lower(self) -> str:
        """Lowercase hex for case-insensitive keys."""
        return self.value.lower()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Address):
            return False
        return self.lower == other.lower

    def __hash__(self) -> int:
        return hash(self.lower)

    def __repr__(self) -> str:
        return f"Address({self.value})"


@dataclass(frozen=True)
class TokenAmount:
    """Fixed-point token amount: integer raw units + decimals + optional symbol."""

    raw: int
    decimals: int
    symbol: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.raw, float):
            raise TokenMathError("Floating point numbers are strictly forbidden in TokenAmount.")
        object.__setattr__(self, "raw", int(self.raw))
        object.__setattr__(self, "decimals", int(self.decimals))
        if self.decimals < 0:
            raise TokenMathError("decimals must be non-negative")

    @classmethod
    def from_human(cls, amount: str | Decimal, decimals: int, symbol: str = None) -> TokenAmount:
        """
        Args:
            amount: Human-readable quantity (string or Decimal, not float).
            decimals: Token decimals (e.g. 18 for ETH).
            symbol: Optional ticker for display.

        Returns:
            :class:`TokenAmount` in atomic units.
        """
        if isinstance(amount, float):
            raise TokenMathError(
                "Cannot initialize TokenAmount from a float. Use strings or Decimals."
            )
        try:
            dec_amount = Decimal(amount)
        except InvalidOperation:
            raise TokenMathError(f"Invalid human amount provided: {amount}") from None

        with _high_precision():
            multiplier = Decimal(10**decimals)
            raw_amount = int(dec_amount * multiplier)
        return cls(raw=raw_amount, decimals=decimals, symbol=symbol)

    @property
    def human(self) -> Decimal:
        """Human-readable :class:`Decimal` (not rounded for display)."""
        return Decimal(self.raw) / Decimal(10**self.decimals)

    def _check_compatible(self, other: TokenAmount) -> None:
        if not isinstance(other, TokenAmount):
            raise TypeError("Operand must be a TokenAmount.")
        if self.decimals != other.decimals:
            raise TokenMathError(
                f"Cannot operate on tokens with different decimals: "
                f"{self.decimals} vs {other.decimals}"
            )

    def __add__(self, other: TokenAmount) -> TokenAmount:
        self._check_compatible(other)
        symbol = self.symbol or other.symbol
        return TokenAmount(raw=self.raw + other.raw, decimals=self.decimals, symbol=symbol)

    def __sub__(self, other: TokenAmount) -> TokenAmount:
        self._check_compatible(other)
        symbol = self.symbol or other.symbol
        return TokenAmount(raw=self.raw - other.raw, decimals=self.decimals, symbol=symbol)

    def __mul__(self, factor: int | Decimal) -> TokenAmount:
        if isinstance(factor, float):
            raise TokenMathError("Floating point math forbidden. Use int, str, or Decimal.")
        if isinstance(factor, int) and not isinstance(factor, bool):
            new_raw = self.raw * factor
        else:
            with _high_precision():
                new_raw = int(Decimal(self.raw) * Decimal(factor))
        return TokenAmount(raw=new_raw, decimals=self.decimals, symbol=self.symbol)

    def __rmul__(self, factor: int | Decimal) -> TokenAmount:
        return self.__mul__(factor)

    def __str__(self) -> str:
        return f"{self.human} {self.symbol}" if self.symbol else str(self.human)


@dataclass(frozen=True, eq=False)
class Token:
    """ERC-20 token identity: address is the only field used in ``__eq__``/``__hash__``."""

    address: Address
    symbol: str
    decimals: int

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise WalletValidationError("Token.symbol must be a string.")
        if not isinstance(self.decimals, int) or isinstance(self.decimals, bool):
            raise WalletValidationError("Token.decimals must be an integer.")
        if self.decimals < 0:
            raise TokenMathError("Token.decimals must be non-negative.")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Token):
            return self.address == other.address
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.address.lower)

    def __repr__(self) -> str:
        return f"Token({self.symbol},{self.address.checksum})"


@dataclass
class TransactionRequest:
    """Unsigned EIP-1559-style transaction fields for signing (Web3-compatible dict round-trip)."""

    to: Address
    value: TokenAmount
    data: bytes
    nonce: Optional[int] = None
    gas_limit: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee: Optional[int] = None
    chain_id: int = 1
    from_address: Optional[str] = None

    @classmethod
    def from_dict(cls, tx: dict[str, Any]) -> TransactionRequest:
        """
        Args:
            tx: Web3-style dict (``to``, ``value``, ``data``, ``gas``, EIP-1559 fees, etc.).

        Returns:
            Parsed :class:`TransactionRequest`.

        Raises:
            ValueError: Invalid or missing ``to``.
        """
        if not isinstance(tx, dict):
            raise WalletValidationError("Transaction must be a dictionary.")
        to_addr = _parse_to_address(tx.get("to"))
        value = _parse_value_field(tx.get("value", 0))
        raw_data = tx.get("data", b"")
        if raw_data is None:
            raise WalletValidationError("Transaction 'data' must not be None.")
        if isinstance(raw_data, str):
            raise WalletValidationError(
                "Transaction 'data' must be bytes-like, not a str (hex calldata should be bytes)."
            )
        if not isinstance(raw_data, (bytes, bytearray, memoryview)):
            raise WalletValidationError("Transaction 'data' must be bytes-like.")
        raw_data = bytes(raw_data)
        req = cls(
            to=to_addr,
            value=value,
            data=raw_data,
            nonce=tx.get("nonce"),
            gas_limit=tx.get("gas"),
            max_fee_per_gas=tx.get("maxFeePerGas"),
            max_priority_fee=tx.get("maxPriorityFeePerGas"),
            chain_id=tx.get("chainId", 1),
            from_address=tx.get("from"),
        )
        logger.debug(
            "TransactionRequest from_dict: chain_id=%s has_nonce=%s",
            req.chain_id,
            req.nonce is not None,
        )
        return req

    def to_dict(self) -> dict[str, Any]:
        """Build a dict suitable for ``eth_signTransaction`` / ``sign_transaction``."""
        tx: dict[str, Any] = {
            "to": self.to.checksum,
            "value": self.value.raw,
            "data": self.data,
            "chainId": self.chain_id,
        }
        if self.from_address is not None:
            tx["from"] = self.from_address
        if self.nonce is not None:
            tx["nonce"] = self.nonce
        if self.gas_limit is not None:
            tx["gas"] = self.gas_limit
        if self.max_fee_per_gas is not None:
            tx["maxFeePerGas"] = self.max_fee_per_gas
        if self.max_priority_fee is not None:
            tx["maxPriorityFeePerGas"] = self.max_priority_fee

        return tx


@dataclass
class TransactionReceipt:
    """Subset of fields from an ``eth_getTransactionReceipt`` response."""

    tx_hash: str
    block_number: int
    status: bool
    gas_used: int
    effective_gas_price: int
    logs: list

    @property
    def tx_fee(self) -> TokenAmount:
        """Approximate fee as ``gas_used * effective_gas_price`` (18-decimal wei)."""
        raw_fee = self.gas_used * self.effective_gas_price
        return TokenAmount(raw=raw_fee, decimals=18, symbol="GAS")

    @classmethod
    def from_web3(cls, receipt: dict[str, Any]) -> TransactionReceipt:
        """
        Args:
            receipt: AttributeDict or dict from Web3 (snake_case keys).

        Returns:
            Normalized :class:`TransactionReceipt`.
        """
        return cls(
            tx_hash=_receipt_tx_hash_hex(receipt),
            block_number=receipt["blockNumber"],
            status=receipt["status"] == 1,
            gas_used=receipt["gasUsed"],
            effective_gas_price=receipt["effectiveGasPrice"],
            logs=receipt["logs"],
        )
