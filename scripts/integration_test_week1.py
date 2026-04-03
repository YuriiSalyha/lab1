"""Week 1 — Sepolia integration: smoke transfer + pytest edge cases in one module.

Smoke (wallet → build → sign → send → confirm) then edge-case tests (RPC failover,
timeouts, nonce, insufficient balance, OOG, replacements).

Run everything::

    python scripts/integration_test_week1.py

Or::

    .\\run.ps1 integration

Edge cases only (no smoke)::

    pytest scripts/integration_test_week1.py -v

Environment:
    ``PRIVATE_KEY`` — required (funded Sepolia wallet).
    ``SEPOLIA_RPC`` / ``RPC_ENDPOINT`` — optional RPC (default public Sepolia).
    ``TEST_RECIPIENT`` — optional; defaults to burn address.
    ``SEPOLIA_WETH`` — optional; for OOG deposit test (default WETH address).
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest
from dotenv import load_dotenv
from eth_account import Account

from chain.builder import TransactionBuilder
from chain.client import ChainClient
from chain.errors import ChainError, InsufficientFunds, NonceTooLow, TransactionTimeout
from core.errors import WalletError
from core.logging_config import configure_project_logging
from core.types import Address, TokenAmount
from core.wallet import WalletManager

load_dotenv()

# ── Shared constants ───────────────────────────────────────────────────────────
SEPOLIA_CHAIN_ID = 11155111
TRANSFER_ETH = "0.001"
MIN_BALANCE_ETH = Decimal("0.001")
DEFAULT_RECIPIENT = "0x000000000000000000000000000000000000dEaD"
DEFAULT_RPC = "https://rpc.sepolia.org"
CONFIRMATION_TIMEOUT = 180

DEFAULT_SEPOLIA_WETH = "0xfff9976782d46cc05630d1f6ebab18b2324d6b14"
BURN = "0x000000000000000000000000000000000000dEaD"
WETH_DEPOSIT_SELECTOR = bytes.fromhex("d0e30db0")

pytestmark = pytest.mark.integration


def _rpc_url() -> str:
    return os.getenv("SEPOLIA_RPC") or os.getenv("RPC_ENDPOINT") or "https://rpc.sepolia.org"


# ── Pytest fixtures (used when running ``pytest`` on this file) ───────────────
@pytest.fixture(scope="module")
def integration_wallet() -> WalletManager:
    if not os.getenv("PRIVATE_KEY"):
        pytest.skip("Set PRIVATE_KEY to run integration tests")
    return WalletManager.from_env("PRIVATE_KEY")


@pytest.fixture
def wallet(integration_wallet: WalletManager) -> WalletManager:
    return integration_wallet


@pytest.fixture(scope="module")
def integration_rpc_url() -> str:
    return _rpc_url()


@pytest.fixture
def rpc_url(integration_rpc_url: str) -> str:
    return integration_rpc_url


@pytest.fixture
def client(integration_rpc_url: str, integration_wallet: WalletManager) -> ChainClient:
    assert integration_wallet.address.startswith("0x")
    c = ChainClient([integration_rpc_url])
    if c.w3.eth.chain_id != SEPOLIA_CHAIN_ID:
        pytest.skip(f"Expected Sepolia (chain {SEPOLIA_CHAIN_ID}), got {c.w3.eth.chain_id}")
    return c


def _min_balance_wei(client: ChainClient, wallet: WalletManager, min_eth: str) -> None:
    bal = client.get_balance(Address(wallet.address)).human
    if bal < Decimal(min_eth):
        pytest.skip(f"Need at least {min_eth} ETH on Sepolia for this test (have {bal})")


# ── Edge-case tests ────────────────────────────────────────────────────────────


def test_rpc_fallback_uses_second_endpoint(wallet: WalletManager, rpc_url: str) -> None:
    assert wallet.address.startswith("0x")
    bad_first = "http://127.0.0.1:1"
    cl = ChainClient([bad_first, rpc_url], timeout_seconds=15, max_retries=3)
    bal = cl.get_balance(Address("0x0000000000000000000000000000000000000000"))
    assert bal.raw >= 0


def test_wait_for_receipt_timeout_raises_transaction_timeout(
    client: ChainClient, wallet: WalletManager
) -> None:
    _ = wallet
    fake_hash = "0x" + "00" * 32
    with pytest.raises(TransactionTimeout) as exc:
        client.wait_for_receipt(fake_hash, timeout_seconds=2, poll_interval_seconds=1)
    assert exc.value.tx_hash == fake_hash
    assert exc.value.timeout_seconds == 2


def test_nonce_too_low_on_broadcast(client: ChainClient, wallet: WalletManager) -> None:
    _min_balance_wei(client, wallet, "0.0001")
    current = client.w3.eth.get_transaction_count(wallet.address, "latest")
    if current == 0:
        pytest.skip("Need nonce >= 1 to assert nonce-too-low")

    gp = client.get_gas_price()
    builder = (
        TransactionBuilder(client, wallet)
        .to(Address(BURN))
        .value(TokenAmount.from_human("0.0000001", 18, "ETH"))
        .nonce(current - 1)
        .gas_limit(21000)
    )
    builder._tx["maxFeePerGas"] = gp.get_max_fee("high")
    builder._tx["maxPriorityFeePerGas"] = gp.get_priority_fee("high")

    with pytest.raises(NonceTooLow):
        signed = builder.build_and_sign()
        client.send_raw_transaction(signed.raw_transaction)


def test_insufficient_funds_for_value_plus_max_gas(
    client: ChainClient, wallet: WalletManager
) -> None:
    addr = Address(wallet.address)
    bal = client.get_balance(addr).raw
    gp = client.get_gas_price()
    max_fee = gp.get_max_fee("medium")
    gas_limit = 21000
    max_gas_cost = gas_limit * max_fee
    value_raw = bal - max_gas_cost + 1
    if value_raw <= 0:
        pytest.skip("Balance too small to construct value+gas > balance")

    builder = (
        TransactionBuilder(client, wallet)
        .to(Address(BURN))
        .value(TokenAmount(raw=value_raw, decimals=18, symbol="ETH"))
        .gas_limit(gas_limit)
        .with_gas_price("medium")
    )
    with pytest.raises(InsufficientFunds):
        builder.build_and_sign()


def test_out_of_gas_contract_call(client: ChainClient, wallet: WalletManager, rpc_url: str) -> None:
    _ = rpc_url
    _min_balance_wei(client, wallet, "0.0002")
    weth = os.getenv("SEPOLIA_WETH", DEFAULT_SEPOLIA_WETH)
    gas_limit = 25_000
    builder = (
        TransactionBuilder(client, wallet)
        .to(Address(weth))
        .value(TokenAmount.from_human("0.00000001", 18, "ETH"))
        .data(WETH_DEPOSIT_SELECTOR)
        .gas_limit(gas_limit)
    )
    builder._tx["maxFeePerGas"] = client.get_gas_price().get_max_fee("high")
    builder._tx["maxPriorityFeePerGas"] = client.get_gas_price().get_priority_fee("high")

    signed = builder.build_and_sign()
    tx_hash = client.send_raw_transaction(signed.raw_transaction)
    receipt = client.wait_for_receipt(tx_hash, timeout_seconds=180)
    assert receipt.status is False
    # Receipt has no gas_limit; OOG typically uses all gas supplied on the tx.
    assert receipt.gas_used >= gas_limit - 1


def test_transaction_replacement_speed_up(client: ChainClient, wallet: WalletManager) -> None:
    _min_balance_wei(client, wallet, "0.002")
    burn = Address(BURN)
    latest = client.w3.eth.get_block("latest")
    base_fee = latest["baseFeePerGas"]
    prio_low = 1_000_000_000
    prio_high = 4_000_000_000
    max_low = base_fee + prio_low
    max_high = base_fee + prio_high

    nonce = client.w3.eth.get_transaction_count(wallet.address, "latest")
    val = TokenAmount.from_human("0.00001", 18, "ETH")

    b1 = TransactionBuilder(client, wallet).to(burn).value(val).nonce(nonce).gas_limit(21000)
    b1._tx["maxFeePerGas"] = max_low
    b1._tx["maxPriorityFeePerGas"] = prio_low
    h1 = client.send_raw_transaction(b1.build_and_sign().raw_transaction)

    fresh = ChainClient(client.rpc_urls, timeout_seconds=client.timeout_seconds)
    b2 = TransactionBuilder(fresh, wallet).to(burn).value(val).nonce(nonce).gas_limit(21000)
    b2._tx["maxFeePerGas"] = max_high
    b2._tx["maxPriorityFeePerGas"] = prio_high
    h2 = client.send_raw_transaction(b2.build_and_sign().raw_transaction)

    rec = client.wait_for_receipt(h2, timeout_seconds=180)
    assert rec.status is True
    assert h1 != h2


def test_transaction_cancellation_self_transfer(client: ChainClient, wallet: WalletManager) -> None:
    _min_balance_wei(client, wallet, "0.002")
    self_addr = Address(wallet.address)
    latest = client.w3.eth.get_block("latest")
    base_fee = latest["baseFeePerGas"]
    prio_low = 1_000_000_000
    prio_high = 5_000_000_000
    nonce = client.w3.eth.get_transaction_count(wallet.address, "latest")

    b1 = (
        TransactionBuilder(client, wallet)
        .to(Address(BURN))
        .value(TokenAmount.from_human("0.00001", 18, "ETH"))
        .nonce(nonce)
        .gas_limit(21000)
    )
    b1._tx["maxFeePerGas"] = base_fee + prio_low
    b1._tx["maxPriorityFeePerGas"] = prio_low
    client.send_raw_transaction(b1.build_and_sign().raw_transaction)

    fresh = ChainClient(client.rpc_urls, timeout_seconds=client.timeout_seconds)
    b2 = (
        TransactionBuilder(fresh, wallet)
        .to(self_addr)
        .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
        .nonce(nonce)
        .gas_limit(21000)
    )
    b2._tx["maxFeePerGas"] = base_fee + prio_high
    b2._tx["maxPriorityFeePerGas"] = prio_high
    h2 = client.send_raw_transaction(b2.build_and_sign().raw_transaction)
    rec = client.wait_for_receipt(h2, timeout_seconds=180)
    assert rec.status is True


# ── Smoke path ────────────────────────────────────────────────────────────────


def _wei_to_eth(wei: int) -> Decimal:
    return Decimal(wei) / Decimal(10**18)


def _wei_to_gwei(wei: int) -> Decimal:
    return Decimal(wei) / Decimal(10**9)


def _run_smoke(rpc_url: str, recipient: str) -> int:
    wallet = WalletManager.from_env("PRIVATE_KEY")
    print(f"Wallet: {wallet.address}")

    client = ChainClient([rpc_url])
    chain_id = client.w3.eth.chain_id
    if chain_id != SEPOLIA_CHAIN_ID:
        print(
            f"\nERROR: Expected Sepolia (chain {SEPOLIA_CHAIN_ID}) but got chain {chain_id}.",
            file=sys.stderr,
        )
        print("Integration test FAILED")
        return 1
    print(f"Connected to Sepolia (chain {chain_id})")

    balance = client.get_balance(Address(wallet.address))
    balance_eth = balance.human
    print(f"Balance: {balance_eth:.6f} ETH")

    if balance_eth < MIN_BALANCE_ETH:
        print(
            f"\nERROR: Balance too low ({balance_eth} ETH). "
            f"Need at least {MIN_BALANCE_ETH} ETH on Sepolia.\n"
            f"Use a faucet: https://sepoliafaucet.com/",
            file=sys.stderr,
        )
        print("\nIntegration test FAILED")
        return 1

    print("\nBuilding transaction...")
    transfer_value = TokenAmount.from_human(TRANSFER_ETH, decimals=18, symbol="ETH")
    to_addr = Address(recipient)

    builder = (
        TransactionBuilder(client, wallet)
        .to(to_addr)
        .value(transfer_value)
        .with_gas_estimate()
        .with_gas_price(priority="medium")
    )

    tx_dict = builder._tx
    gas_limit = tx_dict["gas"]
    max_fee = tx_dict["maxFeePerGas"]
    max_priority = tx_dict["maxPriorityFeePerGas"]

    print(f"  To: {to_addr.checksum}")
    print(f"  Value: {TRANSFER_ETH} ETH")
    print(f"  Estimated Gas: {gas_limit}")
    print(f"  Max Fee: {_wei_to_gwei(max_fee):.1f} gwei")
    print(f"  Max Priority: {_wei_to_gwei(max_priority):.1f} gwei")

    print("\nSigning...")
    signed_tx = builder.build_and_sign()

    recovered = Account.recover_transaction(signed_tx.raw_transaction)
    sig_valid = recovered.lower() == wallet.address.lower()
    print(f"  Signature valid: {'YES' if sig_valid else 'NO'}")
    print(f"  Recovered address matches: {'YES' if sig_valid else 'NO'}")
    if not sig_valid:
        print(
            f"\nERROR: Recovered address {recovered} does not match wallet {wallet.address}",
            file=sys.stderr,
        )
        print("\nIntegration test FAILED")
        return 1

    print("\nSending...")
    tx_hash = client.send_raw_transaction(signed_tx.raw_transaction)
    print(f"  TX Hash: {tx_hash}")

    print("\nWaiting for confirmation...")
    receipt = client.wait_for_receipt(tx_hash, timeout_seconds=CONFIRMATION_TIMEOUT)

    status_label = "SUCCESS" if receipt.status else "REVERTED"
    gas_used = receipt.gas_used
    efficiency = (gas_used / gas_limit * 100) if gas_limit else 0
    fee_eth = _wei_to_eth(receipt.gas_used * receipt.effective_gas_price)

    print(f"  Block: {receipt.block_number}")
    print(f"  Status: {status_label}")
    print(f"  Gas Used: {gas_used} ({efficiency:.0f}%)")
    print(f"  Fee: {fee_eth:.6f} ETH")

    if not receipt.status:
        print("\nIntegration test FAILED (transaction reverted)")
        return 1

    print("\nIntegration test PASSED")
    return 0


def run_smoke() -> int:
    configure_project_logging()
    rpc_url = os.getenv("SEPOLIA_RPC") or os.getenv("RPC_ENDPOINT") or DEFAULT_RPC
    recipient = os.getenv("TEST_RECIPIENT", DEFAULT_RECIPIENT)
    try:
        return _run_smoke(rpc_url, recipient)
    except (ChainError, WalletError) as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        print("\nIntegration test FAILED")
        return 1
    except KeyboardInterrupt:
        print("\nAborted by user")
        return 130


def main() -> int:
    """Run smoke integration, then pytest edge cases in this file."""
    code = run_smoke()
    if code != 0:
        return code
    return pytest.main([__file__, "-v", "--tb=short"])


if __name__ == "__main__":
    sys.exit(main())
