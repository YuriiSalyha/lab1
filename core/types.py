from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Optional

from eth_utils import is_address, to_checksum_address

from core.errors import InvalidAddressError, TokenMathError, WalletValidationError

# 78 digits covers uint256 (2^256 ≈ 1.16e77) with room to spare.
_DECIMAL_PRECISION = 78


@contextmanager
def _high_precision():
    """Temporary Decimal context with enough precision for on-chain math."""
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        yield ctx


@dataclass(frozen=True)
class Address:
    """Ethereum address with validation and checksumming."""

    value: str

    def __post_init__(self):
        # Validate and convert to checksum
        if not isinstance(self.value, str):
            raise WalletValidationError("Address must be a string")
        if not self.value.startswith("0x"):
            raise InvalidAddressError(f"Address must start with '0x': {self.value}")
        if not is_address(self.value):
            raise InvalidAddressError(f"Invalid Ethereum address: {self.value}")
        object.__setattr__(self, "value", to_checksum_address(self.value))

    @classmethod
    def from_string(cls, s: str) -> "Address":
        return cls(value=s)

    @property
    def checksum(self) -> str:
        return self.value

    @property
    def lower(self) -> str:
        return self.value.lower()

    def __eq__(self, other) -> bool:
        if not isinstance(other, Address):
            return False
        return self.lower == other.lower

    def __hash__(self) -> int:
        return hash(self.lower)

    def __repr__(self) -> str:
        return f"Address({self.value})"


@dataclass(frozen=True)
class TokenAmount:
    """
    Represents a token amount with proper decimal handling.

    Internally stores raw integer (wei-equivalent).
    Provides human-readable formatting.
    """

    raw: int  # Raw amount (e.g., wei)
    decimals: int  # Token decimals (e.g., 18 for ETH, 6 for USDC)
    symbol: Optional[str] = None

    def __post_init__(self):
        # Ensure strict typing to catch developer errors early
        if isinstance(self.raw, float):
            raise TokenMathError("Floating point numbers are strictly forbidden in TokenAmount.")
        object.__setattr__(self, "raw", int(self.raw))
        object.__setattr__(self, "decimals", int(self.decimals))
        if self.decimals < 0:
            raise TokenMathError("decimals must be non-negative")

    @classmethod
    def from_human(cls, amount: str | Decimal, decimals: int, symbol: str = None) -> "TokenAmount":
        """Create from human-readable amount (e.g., '1.5' ETH)."""
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
        """Returns human-readable decimal."""
        return Decimal(self.raw) / Decimal(10**self.decimals)

    def _check_compatible(self, other: "TokenAmount") -> None:
        if not isinstance(other, TokenAmount):
            raise TypeError("Operand must be a TokenAmount.")
        if self.decimals != other.decimals:
            raise TokenMathError(
                f"Cannot operate on tokens with different decimals: "
                f"{self.decimals} vs {other.decimals}"
            )

    def __add__(self, other: "TokenAmount") -> "TokenAmount":
        self._check_compatible(other)
        symbol = self.symbol or other.symbol
        return TokenAmount(raw=self.raw + other.raw, decimals=self.decimals, symbol=symbol)

    def __sub__(self, other: "TokenAmount") -> "TokenAmount":
        self._check_compatible(other)
        symbol = self.symbol or other.symbol
        return TokenAmount(raw=self.raw - other.raw, decimals=self.decimals, symbol=symbol)

    def __mul__(self, factor: int | Decimal) -> "TokenAmount":
        if isinstance(factor, float):
            raise TokenMathError("Floating point math forbidden. Use int, str, or Decimal.")
        if isinstance(factor, int) and not isinstance(factor, bool):
            new_raw = self.raw * factor
        else:
            # Decimal factor: use precision large enough for 2^256-scale values
            with _high_precision():
                new_raw = int(Decimal(self.raw) * Decimal(factor))
        return TokenAmount(raw=new_raw, decimals=self.decimals, symbol=self.symbol)

    def __rmul__(self, factor: int | Decimal) -> "TokenAmount":
        return self.__mul__(factor)

    def __str__(self) -> str:
        return f"{self.human} {self.symbol}" if self.symbol else str(self.human)


@dataclass(frozen=True, eq=False)
class Token:
    """
    Represents an ERC-20 token with its on-chain metadata.

    Identity is by address only — two Token instances at the same address
    are equal regardless of symbol/decimals (those are metadata, not identity).
    We use eq=False to override the dataclass-generated __eq__ and define our own.

    This type will be used extensively from Week 2 onward (AMM math, routing, etc.).
    """

    address: Address
    symbol: str
    decimals: int

    def __eq__(self, other) -> bool:
        if isinstance(other, Token):
            return self.address == other.address  # Delegates to Address.__eq__ (case-insensitive)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.address.lower)

    def __repr__(self) -> str:
        return f"Token({self.symbol},{self.address.checksum})"


@dataclass
class TransactionRequest:
    """A transaction ready to be signed."""

    to: Address
    value: TokenAmount
    data: bytes
    nonce: Optional[int] = None
    gas_limit: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee: Optional[int] = None
    chain_id: int = 1

    def to_dict(self) -> dict:
        """Convert to web3-compatible dict."""
        tx = {
            "to": self.to.checksum,
            "value": self.value.raw,
            "data": self.data,
            "chainId": self.chain_id,
        }
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
    """Parsed transaction receipt."""

    tx_hash: str
    block_number: int
    status: bool  # True = success
    gas_used: int
    effective_gas_price: int
    logs: list

    @property
    def tx_fee(self) -> TokenAmount:
        """Returns transaction fee as TokenAmount."""
        raw_fee = self.gas_used * self.effective_gas_price
        return TokenAmount(raw=raw_fee, decimals=18, symbol="GAS")

    @classmethod
    def from_web3(cls, receipt: dict) -> "TransactionReceipt":
        """Parse from web3 receipt dict."""
        return cls(
            tx_hash=receipt["transactionHash"].hex(),
            block_number=receipt["blockNumber"],
            status=receipt["status"] == 1,
            gas_used=receipt["gasUsed"],
            effective_gas_price=receipt["effectiveGasPrice"],
            logs=receipt["logs"],
        )
