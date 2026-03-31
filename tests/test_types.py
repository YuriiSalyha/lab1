from decimal import Decimal

import pytest

from core.errors import InvalidAddressError, TokenMathError, WalletValidationError
from core.types import Address, Token, TokenAmount, TransactionReceipt, TransactionRequest

# A valid 40-hex-char address (all lowercase)
ADDR_LOWER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
# Same address in mixed case (checksummed form will differ in casing)
ADDR_UPPER = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


class TestAddressValidation:
    """Requirement 1: Address('invalid') raises clear error."""

    def test_totally_invalid_string(self):
        with pytest.raises(InvalidAddressError):
            Address("invalid")

    def test_missing_0x_prefix(self):
        with pytest.raises(InvalidAddressError):
            Address("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_too_short(self):
        with pytest.raises(InvalidAddressError):
            Address("0xabc")

    def test_too_long(self):
        with pytest.raises(InvalidAddressError):
            Address("0x" + "a" * 41)

    def test_invalid_hex_chars(self):
        with pytest.raises(InvalidAddressError):
            Address("0x" + "z" * 40)

    def test_non_string_rejected(self):
        with pytest.raises(WalletValidationError, match="must be a string"):
            Address(12345)

    def test_none_rejected(self):
        with pytest.raises(WalletValidationError, match="must be a string"):
            Address(None)


class TestAddressCaseInsensitiveEquality:
    """Requirement 2: Address('0xabc...') equals Address('0xABC...') (case-insensitive)."""

    def test_lowercase_equals_uppercase(self):
        a = Address(ADDR_LOWER)
        b = Address(ADDR_UPPER)
        assert a == b

    def test_mixed_case_equals(self):
        a = Address("0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa")
        b = Address(ADDR_LOWER)
        assert a == b

    def test_hash_same_for_equal_addresses(self):
        a = Address(ADDR_LOWER)
        b = Address(ADDR_UPPER)
        assert hash(a) == hash(b)

    def test_can_be_used_as_dict_key(self):
        a = Address(ADDR_LOWER)
        b = Address(ADDR_UPPER)
        d = {a: "value"}
        assert d[b] == "value"


class TestAddressProperties:
    def test_checksum_is_eip55(self):
        addr = Address(ADDR_LOWER)
        assert addr.checksum.startswith("0x")
        assert len(addr.checksum) == 42

    def test_lower_returns_lowercase(self):
        addr = Address(ADDR_UPPER)
        assert addr.lower == addr.lower.lower()

    def test_from_string(self):
        addr = Address.from_string(ADDR_LOWER)
        assert isinstance(addr, Address)
        assert addr == Address(ADDR_LOWER)

    def test_repr(self):
        addr = Address(ADDR_LOWER)
        assert "Address(" in repr(addr)

    def test_not_equal_to_non_address(self):
        addr = Address(ADDR_LOWER)
        assert addr != "not an address"
        assert addr != 42


# ---------------------------------------------------------------------------
# TokenAmount
# ---------------------------------------------------------------------------


class TestTokenAmountFromHuman:
    """Requirement 3: TokenAmount.from_human('1.5', 18).raw == 1_500_000_000_000_000_000."""

    def test_1_5_eth(self):
        ta = TokenAmount.from_human("1.5", 18)
        assert ta.raw == 1_500_000_000_000_000_000

    def test_1_0_eth(self):
        ta = TokenAmount.from_human("1.0", 18)
        assert ta.raw == 10**18

    def test_usdc_6_decimals(self):
        ta = TokenAmount.from_human("100", 6)
        assert ta.raw == 100_000_000

    def test_small_fractional(self):
        ta = TokenAmount.from_human("0.000001", 18)
        assert ta.raw == 10**12

    def test_from_decimal_type(self):
        ta = TokenAmount.from_human(Decimal("2.5"), 18)
        assert ta.raw == 2_500_000_000_000_000_000

    def test_zero(self):
        ta = TokenAmount.from_human("0", 18)
        assert ta.raw == 0

    def test_human_property_roundtrips(self):
        ta = TokenAmount.from_human("1.5", 18)
        assert ta.human == Decimal("1.5")

    def test_float_input_rejected(self):
        with pytest.raises(TokenMathError, match="float"):
            TokenAmount.from_human(1.5, 18)

    def test_invalid_string_rejected(self):
        with pytest.raises(TokenMathError, match="Invalid human amount"):
            TokenAmount.from_human("not_a_number", 18)


class TestTokenAmountDifferentDecimals:
    """Requirement 4: Adding TokenAmount with different decimals raises error."""

    def test_add_different_decimals_raises(self):
        eth = TokenAmount(raw=10**18, decimals=18)
        usdc = TokenAmount(raw=10**6, decimals=6)
        with pytest.raises(TokenMathError, match="different decimals"):
            eth + usdc

    def test_sub_different_decimals_raises(self):
        eth = TokenAmount(raw=10**18, decimals=18)
        usdc = TokenAmount(raw=10**6, decimals=6)
        with pytest.raises(TokenMathError, match="different decimals"):
            eth - usdc


class TestTokenAmountNoFloatInternals:
    """Requirement 5: TokenAmount arithmetic never uses float internally."""

    def test_raw_is_always_int(self):
        a = TokenAmount(raw=10**18, decimals=18)
        b = TokenAmount(raw=5 * 10**17, decimals=18)
        result = a + b
        assert isinstance(result.raw, int)

    def test_sub_result_is_int(self):
        a = TokenAmount(raw=10**18, decimals=18)
        b = TokenAmount(raw=3 * 10**17, decimals=18)
        result = a - b
        assert isinstance(result.raw, int)

    def test_mul_result_is_int(self):
        a = TokenAmount(raw=10**18, decimals=18)
        result = a * 3
        assert isinstance(result.raw, int)

    def test_mul_with_decimal_result_is_int(self):
        a = TokenAmount(raw=10**18, decimals=18)
        result = a * Decimal("1.5")
        assert isinstance(result.raw, int)
        assert result.raw == 1_500_000_000_000_000_000

    def test_float_raw_rejected(self):
        with pytest.raises(TokenMathError, match="Floating point"):
            TokenAmount(raw=1.5, decimals=18)

    def test_float_mul_rejected(self):
        a = TokenAmount(raw=10**18, decimals=18)
        with pytest.raises(TokenMathError, match="Floating point math forbidden"):
            a * 1.5

    def test_from_human_never_produces_float_raw(self):
        ta = TokenAmount.from_human("0.1", 18)
        assert isinstance(ta.raw, int)

    def test_large_mul_stays_int(self):
        a = TokenAmount(raw=2**128, decimals=18)
        result = a * 2
        assert isinstance(result.raw, int)
        assert result.raw == 2**129


class TestTokenAmountArithmetic:
    def test_add(self):
        a = TokenAmount(raw=100, decimals=6, symbol="USDC")
        b = TokenAmount(raw=200, decimals=6, symbol="USDC")
        assert (a + b).raw == 300

    def test_sub(self):
        a = TokenAmount(raw=300, decimals=6, symbol="USDC")
        b = TokenAmount(raw=100, decimals=6, symbol="USDC")
        assert (a - b).raw == 200

    def test_mul_int(self):
        a = TokenAmount(raw=100, decimals=6)
        assert (a * 3).raw == 300

    def test_rmul_int(self):
        a = TokenAmount(raw=100, decimals=6)
        assert (3 * a).raw == 300

    def test_add_non_token_amount_raises(self):
        a = TokenAmount(raw=100, decimals=6)
        with pytest.raises(TypeError):
            a + 100

    def test_symbol_inherited_from_left(self):
        a = TokenAmount(raw=100, decimals=6, symbol="USDC")
        b = TokenAmount(raw=200, decimals=6, symbol="DAI")
        assert (a + b).symbol == "USDC"

    def test_symbol_inherited_from_right_if_left_is_none(self):
        a = TokenAmount(raw=100, decimals=6)
        b = TokenAmount(raw=200, decimals=6, symbol="USDC")
        assert (a + b).symbol == "USDC"

    def test_negative_decimals_rejected(self):
        with pytest.raises(TokenMathError, match="non-negative"):
            TokenAmount(raw=100, decimals=-1)


class TestTokenAmountStr:
    def test_with_symbol(self):
        ta = TokenAmount(raw=1_500_000, decimals=6, symbol="USDC")
        assert "1.5" in str(ta)
        assert "USDC" in str(ta)

    def test_without_symbol(self):
        ta = TokenAmount(raw=1_500_000, decimals=6)
        assert "1.5" in str(ta)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

WETH_ADDR = Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
USDC_ADDR = Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")


class TestTokenEqualityByAddress:
    """Requirement 6: Token equality is by address only
    (same address, different symbol -> equal)."""

    def test_same_address_different_symbol_are_equal(self):
        t1 = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        t2 = Token(address=WETH_ADDR, symbol="WrappedEther", decimals=18)
        assert t1 == t2

    def test_same_address_different_decimals_are_equal(self):
        t1 = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        t2 = Token(address=WETH_ADDR, symbol="WETH", decimals=8)
        assert t1 == t2


class TestTokenHashConsistency:
    """Requirement 7: Token hash is consistent with equality (equal tokens have same hash)."""

    def test_equal_tokens_same_hash(self):
        t1 = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        t2 = Token(address=WETH_ADDR, symbol="WrappedEther", decimals=18)
        assert hash(t1) == hash(t2)

    def test_can_be_used_in_set(self):
        t1 = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        t2 = Token(address=WETH_ADDR, symbol="WrappedEther", decimals=18)
        s = {t1, t2}
        assert len(s) == 1

    def test_can_be_used_as_dict_key(self):
        t1 = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        t2 = Token(address=WETH_ADDR, symbol="WrappedEther", decimals=18)
        d = {t1: "value"}
        assert d[t2] == "value"


class TestTokenDifferentAddresses:
    """Requirement 8: Token with different addresses are not equal."""

    def test_different_address_not_equal(self):
        weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
        assert weth != usdc

    def test_different_address_different_hash(self):
        weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        usdc = Token(address=USDC_ADDR, symbol="USDC", decimals=6)
        assert hash(weth) != hash(usdc)

    def test_not_equal_to_non_token(self):
        weth = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        assert weth != "not a token"
        assert weth != 42


class TestTokenRepr:
    def test_repr_contains_symbol_and_address(self):
        t = Token(address=WETH_ADDR, symbol="WETH", decimals=18)
        r = repr(t)
        assert "WETH" in r
        assert WETH_ADDR.checksum in r


# ---------------------------------------------------------------------------
# TransactionRequest
# ---------------------------------------------------------------------------


class TestTransactionRequest:
    def test_to_dict_minimal(self):
        addr = Address(ADDR_LOWER)
        val = TokenAmount(raw=1000, decimals=18)
        tx = TransactionRequest(to=addr, value=val, data=b"")
        d = tx.to_dict()
        assert d["to"] == addr.checksum
        assert d["value"] == 1000
        assert d["data"] == b""
        assert d["chainId"] == 1

    def test_to_dict_all_fields(self):
        addr = Address(ADDR_LOWER)
        val = TokenAmount(raw=0, decimals=18)
        tx = TransactionRequest(
            to=addr,
            value=val,
            data=b"\x00",
            nonce=5,
            gas_limit=21000,
            max_fee_per_gas=30_000_000_000,
            max_priority_fee=1_000_000_000,
            chain_id=42,
        )
        d = tx.to_dict()
        assert d["nonce"] == 5
        assert d["gas"] == 21000
        assert d["maxFeePerGas"] == 30_000_000_000
        assert d["maxPriorityFeePerGas"] == 1_000_000_000
        assert d["chainId"] == 42

    def test_optional_fields_omitted_when_none(self):
        addr = Address(ADDR_LOWER)
        val = TokenAmount(raw=0, decimals=18)
        tx = TransactionRequest(to=addr, value=val, data=b"")
        d = tx.to_dict()
        assert "nonce" not in d
        assert "gas" not in d
        assert "maxFeePerGas" not in d
        assert "maxPriorityFeePerGas" not in d


# ---------------------------------------------------------------------------
# TransactionReceipt
# ---------------------------------------------------------------------------


class TestTransactionReceipt:
    def test_tx_fee(self):
        receipt = TransactionReceipt(
            tx_hash="0xabc",
            block_number=100,
            status=True,
            gas_used=21000,
            effective_gas_price=10**9,
            logs=[],
        )
        fee = receipt.tx_fee
        assert fee.raw == 21000 * 10**9
        assert fee.decimals == 18
        assert fee.symbol == "GAS"

    def test_from_web3(self):
        raw = {
            "transactionHash": bytes.fromhex("aa" * 32),
            "blockNumber": 42,
            "status": 1,
            "gasUsed": 21000,
            "effectiveGasPrice": 10**9,
            "logs": [{"topic": "0x1"}],
        }
        r = TransactionReceipt.from_web3(raw)
        assert r.block_number == 42
        assert r.status is True
        assert r.gas_used == 21000
        assert len(r.logs) == 1

    def test_from_web3_failed_tx(self):
        raw = {
            "transactionHash": bytes.fromhex("bb" * 32),
            "blockNumber": 99,
            "status": 0,
            "gasUsed": 50000,
            "effectiveGasPrice": 2 * 10**9,
            "logs": [],
        }
        r = TransactionReceipt.from_web3(raw)
        assert r.status is False
