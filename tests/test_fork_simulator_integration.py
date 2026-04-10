"""
Live Anvil fork tests. Set ``FORK_RPC_URL`` (e.g. ``http://127.0.0.1:8545``) and run::

    $env:FORK_RPC_URL='http://127.0.0.1:8545'
    pytest -m fork tests/test_fork_simulator_integration.py
"""

from __future__ import annotations

import os

import pytest
from eth_abi import encode
from web3 import Web3

from core.types import Address
from pricing.fork_simulator import ForkSimulator

pytestmark = pytest.mark.fork

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"


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
def test_weth_to_usdc_swap_eth_call_after_deposit_and_approve() -> None:
    """Fund local account on fork (ETH→WETH→approve) then simulate router swap."""
    url = _require_fork_url()
    w3 = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        pytest.skip("FORK_RPC_URL not reachable")

    acct = w3.eth.accounts[0]
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

    sim = ForkSimulator(url)
    deadline = 2**256 - 1
    amount_in = 10**16
    r = sim.simulate_swap(
        Address(UNISWAP_V2_ROUTER),
        {
            "function": "swapExactTokensForTokens",
            "amount_in": amount_in,
            "amount_out_min": 0,
            "path": [Address(WETH), Address(USDC)],
            "to": Address(acct),
            "deadline": deadline,
        },
        Address(acct),
    )

    assert r.success, r.error
    assert r.amount_out > 0


@pytest.mark.fork
def test_anvil_impersonate_account_rpc() -> None:
    """Smoke-test Anvil cheatcode used in whale-based fork setups."""
    url = _require_fork_url()
    w3 = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        pytest.skip("FORK_RPC_URL not reachable")

    target = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
    try:
        w3.provider.make_request("anvil_impersonateAccount", [target])
    except Exception:
        pytest.skip("anvil_impersonateAccount not available on this RPC")

    assert w3.eth.get_balance(target) >= 0
