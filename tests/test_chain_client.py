"""Unit tests for :mod:`chain.client` logic that does not require live RPC."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from eth_abi import encode
from web3.exceptions import ContractLogicError

from chain.client import ChainClient, GasPrice, _format_contract_logic_revert
from chain.errors import (
    GasEstimationFailed,
    InsufficientFunds,
    InvalidParameterError,
    NonceTooHigh,
    NonceTooLow,
    ReplacementUnderpriced,
    RPCError,
)


def test_gas_price_get_max_fee_integer_math():
    """Buffered base (20%) + medium tip."""
    g = GasPrice(
        base_fee=100,
        priority_fee_low=8,
        priority_fee_medium=10,
        priority_fee_high=15,
    )
    # base 100 * 12000/10000 = 120, + tip 10 = 130
    assert g.get_max_fee("medium", buffer_bps=2000) == 130


def test_gas_price_priority_tiers():
    g = GasPrice(base_fee=0, priority_fee_low=1, priority_fee_medium=2, priority_fee_high=3)
    assert g.get_priority_fee("low") == 1
    assert g.get_priority_fee("medium") == 2
    assert g.get_priority_fee("high") == 3


@pytest.mark.parametrize(
    "msg,expected_type",
    [
        ("insufficient funds for gas", InsufficientFunds),
        ("insufficient balance", InsufficientFunds),
        ("nonce too low", NonceTooLow),
        ("already known", NonceTooLow),
        ("nonce too high", NonceTooHigh),
        ("replacement transaction underpriced", ReplacementUnderpriced),
        ("execution reverted: foo", GasEstimationFailed),
    ],
)
def test_classify_error_maps_strings(msg: str, expected_type: type):
    err = Exception(msg)
    classified = ChainClient._classify_error(err)
    assert isinstance(classified, expected_type)


def test_classify_error_returns_none_for_unknown():
    assert ChainClient._classify_error(Exception("random network glitch")) is None


def test_format_contract_logic_revert_decodes_data():
    payload = bytes.fromhex("08c379a0") + encode(["string"], ["slippage"])
    err = ContractLogicError("execution reverted: slippage", data="0x" + payload.hex())
    assert _format_contract_logic_revert(err) == "execution reverted: slippage"


def test_chain_client_requires_at_least_one_rpc():
    with pytest.raises(InvalidParameterError, match="At least one RPC URL"):
        ChainClient(rpc_urls=[])


# ── _execute_with_retry tests (mocked providers) ────────────────────


class TestExecuteWithRetry:
    """Retry behaviour without a real RPC connection."""

    @staticmethod
    def _build_client_with_stubs(results_per_endpoint: list[list]):
        """Build a ``ChainClient`` whose ``_web3_instances`` are stubs.

        *results_per_endpoint* is a list (one per endpoint) of lists.  Each
        inner list contains return values or exceptions **in call order** for
        ``w3.eth.<func>``.  Supplying an ``Exception`` instance causes a raise;
        anything else is returned directly.
        """
        client = ChainClient.__new__(ChainClient)
        client.rpc_urls = [f"http://node-{i}" for i in range(len(results_per_endpoint))]
        client.max_retries = 3
        client.timeout_seconds = 1
        client._nonce_managers = {}

        instances = []
        for endpoint_results in results_per_endpoint:
            w3 = MagicMock()
            stub = MagicMock(side_effect=endpoint_results)
            w3.eth.get_balance = stub
            instances.append(w3)

        client._web3_instances = instances
        client.w3 = instances[0]
        return client

    def test_success_on_first_try(self):
        client = self._build_client_with_stubs([[100]])
        result = client._execute_with_retry("get_balance", "0xabc")
        assert result == 100

    def test_falls_to_second_endpoint_on_failure(self):
        client = self._build_client_with_stubs(
            [
                [ConnectionError("node down")],
                [200],
            ]
        )
        result = client._execute_with_retry("get_balance", "0xabc")
        assert result == 200

    @patch("chain.client.time.sleep")
    def test_retries_across_cycles(self, mock_sleep):
        client = self._build_client_with_stubs(
            [
                [ConnectionError("fail"), ConnectionError("fail"), 300],
            ]
        )
        result = client._execute_with_retry("get_balance", "0xabc")
        assert result == 300
        assert mock_sleep.call_count >= 1

    @patch("chain.client.time.sleep")
    def test_exhausts_retries_raises_rpc_error(self, mock_sleep):
        client = self._build_client_with_stubs(
            [
                [ConnectionError("down")] * 3,
            ]
        )
        with pytest.raises(RPCError, match="failed after 3 retries"):
            client._execute_with_retry("get_balance", "0xabc")

    def test_non_retryable_error_raises_immediately(self):
        client = self._build_client_with_stubs(
            [
                [Exception("insufficient funds for gas")],
            ]
        )
        with pytest.raises(InsufficientFunds):
            client._execute_with_retry("get_balance", "0xabc")
