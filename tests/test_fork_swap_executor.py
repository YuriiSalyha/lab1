"""
Live Anvil fork: full swap pipeline (preflight eth_call → sign → send → parse logs).

    $env:FORK_RPC_URL='http://127.0.0.1:8545'
    pytest -m fork tests/test_fork_swap_executor.py
"""

from __future__ import annotations

import os

import pytest
from eth_abi import encode
from web3 import Web3

from chain.client import ChainClient
from core.types import Address
from core.wallet import WalletManager
from pricing.fork_swap_executor import ForkSwapError, execute_swap_exact_tokens_for_tokens_on_fork
from pricing.route import Route
from pricing.uniswap_v2_pair import UniswapV2Pair

pytestmark = pytest.mark.fork

# Foundry default account #0 (matches ``anvil`` unlocked accounts[0]).
ANVIL_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
# Uniswap V2 WETH/USDC — token0 = USDC, token1 = WETH
PAIR_WETH_USDC = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")


def _require_fork_url() -> str:
    url = os.environ.get("FORK_RPC_URL", "").strip()
    if not url:
        pytest.skip("FORK_RPC_URL not set (start Anvil and export FORK_RPC_URL)")
    return url


def _weth_deposit_data() -> bytes:
    return bytes.fromhex("d0e30db0")


def _erc20_approve_data(spender: str, amount: int) -> bytes:
    return bytes.fromhex("095ea7b3") + encode(
        ["address", "uint256"],
        [Web3.to_checksum_address(spender), amount],
    )


@pytest.mark.fork
def test_execute_swap_preflight_sign_send_parse_logs() -> None:
    """WETH deposit + approve, then :func:`execute_swap_exact_tokens_for_tokens_on_fork`."""
    url = _require_fork_url()
    w3 = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        pytest.skip("FORK_RPC_URL not reachable")

    acct = w3.eth.accounts[0]
    wallet = WalletManager(ANVIL_PRIVATE_KEY)
    assert Web3.to_checksum_address(wallet.address) == Web3.to_checksum_address(acct)

    wrap_wei = 10**17
    tx_dep = {
        "from": acct,
        "to": Web3.to_checksum_address(WETH),
        "value": wrap_wei,
        "data": _weth_deposit_data(),
        "gas": 150_000,
    }
    h = w3.eth.send_transaction(tx_dep)
    w3.eth.wait_for_transaction_receipt(h)

    max_u256 = 2**256 - 1
    tx_ap = {
        "from": acct,
        "to": Web3.to_checksum_address(WETH),
        "data": _erc20_approve_data(UNISWAP_V2_ROUTER, max_u256),
        "gas": 100_000,
    }
    h2 = w3.eth.send_transaction(tx_ap)
    w3.eth.wait_for_transaction_receipt(h2)

    fork_client = ChainClient([url])
    pair = UniswapV2Pair.from_chain(PAIR_WETH_USDC, fork_client)
    # path: WETH → USDC (token1 → token0 on this pair)
    route = Route([pair], [pair.token1, pair.token0])

    amount_in = 10**16
    deadline = 2**256 - 1
    router = Address(UNISWAP_V2_ROUTER)

    out = execute_swap_exact_tokens_for_tokens_on_fork(
        fork_client,
        wallet,
        router,
        route,
        amount_in,
        0,
        deadline,
        run_preflight=True,
    )

    assert out.preflight is not None
    assert out.preflight.success
    assert out.receipt.status
    assert out.tx_hash == out.receipt.tx_hash
    assert len(out.parsed_events) > 0
    names = {e.get("name") for e in out.parsed_events}
    assert "Transfer" in names or "Swap" in names


@pytest.mark.fork
def test_preflight_failure_raises_fork_swap_error() -> None:
    """Absurd amount_in should revert on eth_call and raise ForkSwapError before send."""
    url = _require_fork_url()
    w3 = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        pytest.skip("FORK_RPC_URL not reachable")

    fork_client = ChainClient([url])
    pair = UniswapV2Pair.from_chain(PAIR_WETH_USDC, fork_client)
    route = Route([pair], [pair.token1, pair.token0])
    wallet = WalletManager(ANVIL_PRIVATE_KEY)
    router = Address(UNISWAP_V2_ROUTER)
    deadline = 2**256 - 1

    with pytest.raises(ForkSwapError, match="Preflight"):
        execute_swap_exact_tokens_for_tokens_on_fork(
            fork_client,
            wallet,
            router,
            route,
            10**40,
            0,
            deadline,
            run_preflight=True,
        )
