"""Unit tests for :mod:`chain.nonce_manager` (mocked Web3)."""

from __future__ import annotations

from chain.nonce_manager import NonceManager


class _MockEth:
    def __init__(self, pending_counts: list[int]) -> None:
        self._pending_counts = pending_counts
        self._call_idx = 0

    def get_transaction_count(self, address: str, block: str) -> int:
        assert block == "pending"
        i = min(self._call_idx, len(self._pending_counts) - 1)
        v = self._pending_counts[i]
        self._call_idx += 1
        return v


class _MockWeb3:
    def __init__(self, eth: _MockEth) -> None:
        self.eth = eth


def test_nonce_manager_monotonic_with_fixed_chain_count():
    """When chain pending count stays 10, nonces increment 10, 11, 12, ..."""
    eth = _MockEth([10, 10, 10, 10])
    w3 = _MockWeb3(eth)
    addr = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    nm = NonceManager(addr, w3)

    assert nm.get_nonce() == 10
    assert nm.get_nonce() == 11
    assert nm.get_nonce() == 12


def test_nonce_manager_resyncs_when_chain_moves_ahead():
    """If chain reports higher pending than local+1, adopt the chain value."""
    eth = _MockEth([5, 5, 20, 20])
    w3 = _MockWeb3(eth)
    addr = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    nm = NonceManager(addr, w3)

    assert nm.get_nonce() == 5
    assert nm.get_nonce() == 6
    # Third call: chain says 20 → max(7, 20) = 20 → return 20
    assert nm.get_nonce() == 20
    assert nm.get_nonce() == 21
