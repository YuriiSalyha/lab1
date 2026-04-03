"""Unit tests for :mod:`chain.builder` (mocked client + wallet, no RPC)."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from chain.builder import TransactionBuilder
from chain.client import GasPrice
from chain.errors import InsufficientFunds, TransactionFailed
from core.types import Address, TokenAmount, TransactionReceipt

SEPOLIA_CHAIN_ID = 11155111
SENDER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
RECIPIENT = Address("0x000000000000000000000000000000000000dEaD")
ONE_ETH = TokenAmount(raw=10**18, decimals=18, symbol="ETH")
GAS_PRICE_FIXTURE = GasPrice(
    base_fee=20_000_000_000,
    priority_fee_low=1_000_000_000,
    priority_fee_medium=2_000_000_000,
    priority_fee_high=5_000_000_000,
)

# ``ChainClient`` normalizes tx hashes to 64 hex digits; use valid-length placeholders in mocks.
MOCK_TX_HASH_OK = "0x" + "ab" * 32
MOCK_TX_HASH_FAIL = "0x" + "cd" * 32


def _make_mocks(balance_raw: int = 10**19):
    """Return ``(client_mock, wallet_mock)`` with sane defaults."""
    client = MagicMock()

    type(client.w3.eth).chain_id = PropertyMock(return_value=SEPOLIA_CHAIN_ID)

    client.get_nonce.return_value = 42
    client.estimate_gas.return_value = 21_000
    client.get_gas_price.return_value = GAS_PRICE_FIXTURE
    client.get_balance.return_value = TokenAmount(raw=balance_raw, decimals=18, symbol="ETH")

    wallet = MagicMock()
    wallet.address = SENDER
    signed = MagicMock()
    signed.raw_transaction = b"\xf8"
    wallet.sign_transaction.return_value = signed

    return client, wallet


def _ready_builder(client=None, wallet=None, **overrides):
    """Return a builder with all required fields populated."""
    if client is None or wallet is None:
        c, w = _make_mocks(**overrides)
        client = client or c
        wallet = wallet or w
    return (
        TransactionBuilder(client, wallet)
        .to(RECIPIENT)
        .value(ONE_ETH)
        .gas_limit(21_000)
        .with_gas_price()
    )


# ── Validation ────────────────────────────────────────────────────────


class TestBuildValidation:
    def test_missing_to_raises(self):
        client, wallet = _make_mocks()
        builder = TransactionBuilder(client, wallet).gas_limit(21_000).with_gas_price()
        with pytest.raises(ValueError, match="to"):
            builder.build()

    def test_missing_gas_raises(self):
        client, wallet = _make_mocks()
        builder = TransactionBuilder(client, wallet).to(RECIPIENT).with_gas_price()
        with pytest.raises(ValueError, match="gas"):
            builder.build()

    def test_missing_fee_params_raises(self):
        client, wallet = _make_mocks()
        builder = TransactionBuilder(client, wallet).to(RECIPIENT).gas_limit(21_000)
        with pytest.raises(ValueError, match="maxFeePerGas"):
            builder.build()


# ── Nonce ─────────────────────────────────────────────────────────────


class TestNonce:
    def test_auto_nonce_calls_client(self):
        builder = _ready_builder()
        tx = builder.build()
        builder.client.get_nonce.assert_called_once()
        assert tx.nonce == 42

    def test_explicit_nonce_skips_client(self):
        builder = _ready_builder()
        builder.nonce(99)
        tx = builder.build()
        builder.client.get_nonce.assert_not_called()
        assert tx.nonce == 99


# ── Gas estimation & pricing ─────────────────────────────────────────


class TestGas:
    def test_with_gas_estimate_applies_buffer(self):
        client, wallet = _make_mocks()
        client.estimate_gas.return_value = 21_000
        builder = (
            TransactionBuilder(client, wallet)
            .to(RECIPIENT)
            .value(ONE_ETH)
            .with_gas_estimate(buffer=1.5)
            .with_gas_price()
        )
        assert builder._tx["gas"] == int(21_000 * 1.5)

    def test_with_gas_estimate_default_buffer(self):
        client, wallet = _make_mocks()
        builder = (
            TransactionBuilder(client, wallet)
            .to(RECIPIENT)
            .value(ONE_ETH)
            .with_gas_estimate()
            .with_gas_price()
        )
        client.estimate_gas.assert_called_once()
        assert builder._tx["gas"] == int(21_000 * 1.2)

    def test_with_gas_price_medium(self):
        builder = _ready_builder()
        assert builder._tx["maxPriorityFeePerGas"] == GAS_PRICE_FIXTURE.get_priority_fee("medium")
        assert builder._tx["maxFeePerGas"] == GAS_PRICE_FIXTURE.get_max_fee("medium")

    def test_with_gas_price_high(self):
        client, wallet = _make_mocks()
        builder = (
            TransactionBuilder(client, wallet)
            .to(RECIPIENT)
            .value(ONE_ETH)
            .gas_limit(21_000)
            .with_gas_price(priority="high")
        )
        assert builder._tx["maxPriorityFeePerGas"] == GAS_PRICE_FIXTURE.get_priority_fee("high")
        assert builder._tx["maxFeePerGas"] == GAS_PRICE_FIXTURE.get_max_fee("high")


# ── Balance validation ────────────────────────────────────────────────


class TestBalanceValidation:
    def test_insufficient_balance_raises(self):
        builder = _ready_builder(balance_raw=100)
        with pytest.raises(InsufficientFunds):
            builder.build_and_sign()

    def test_sufficient_balance_passes(self):
        builder = _ready_builder(balance_raw=10**20)
        signed = builder.build_and_sign()
        assert signed is not None


# ── Sign / send / send_and_wait ───────────────────────────────────────


class TestSignAndSend:
    def test_build_and_sign_calls_wallet(self):
        builder = _ready_builder()
        builder.build_and_sign()
        builder.wallet.sign_transaction.assert_called_once()
        call_dict = builder.wallet.sign_transaction.call_args[0][0]
        assert "from" not in call_dict

    def test_send_broadcasts(self):
        builder = _ready_builder()
        builder.client.send_raw_transaction.return_value = MOCK_TX_HASH_OK
        tx_hash = builder.send()
        builder.client.send_raw_transaction.assert_called_once()
        assert tx_hash == MOCK_TX_HASH_OK

    def test_send_and_wait_success(self):
        builder = _ready_builder()
        builder.client.send_raw_transaction.return_value = MOCK_TX_HASH_OK
        builder.client.wait_for_receipt.return_value = TransactionReceipt(
            tx_hash=MOCK_TX_HASH_OK,
            block_number=100,
            status=True,
            gas_used=21_000,
            effective_gas_price=30_000_000_000,
            logs=[],
        )
        receipt = builder.send_and_wait()
        assert receipt.status is True
        assert receipt.block_number == 100

    def test_send_and_wait_revert_raises(self):
        builder = _ready_builder()
        builder.client.send_raw_transaction.return_value = MOCK_TX_HASH_FAIL
        builder.client.wait_for_receipt.return_value = TransactionReceipt(
            tx_hash=MOCK_TX_HASH_FAIL,
            block_number=200,
            status=False,
            gas_used=21_000,
            effective_gas_price=30_000_000_000,
            logs=[],
        )
        builder.client.get_revert_reason.return_value = "out of gas"
        with pytest.raises(TransactionFailed, match="reverted"):
            builder.send_and_wait()
