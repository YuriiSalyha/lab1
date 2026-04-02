"""Unit tests for :mod:`chain.client` logic that does not require live RPC."""

from __future__ import annotations

import pytest

from chain.client import ChainClient, GasPrice
from chain.errors import (
    GasEstimationFailed,
    InsufficientFunds,
    NonceTooHigh,
    NonceTooLow,
    ReplacementUnderpriced,
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


def test_chain_client_requires_at_least_one_rpc():
    with pytest.raises(ValueError, match="At least one RPC URL"):
        ChainClient(rpc_urls=[])
