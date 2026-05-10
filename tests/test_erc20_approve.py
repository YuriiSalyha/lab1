"""Tests for chain.erc20_approve.

Live runs hit ``TransferHelper: TRANSFER_FROM_FAILED`` whenever the Uniswap
router lacked an allowance on the wallet's input token. ``ensure_router_allowance``
fixes that by sending a one-shot ``approve(spender, max_uint256)`` and caching
the result per process. These tests cover the three control-flow branches
(cached / sufficient / approved) plus the calldata encoding contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from chain import erc20_approve
from chain.erc20_approve import (
    MAX_UINT256,
    _encode_approve,
    ensure_router_allowance,
    reset_approved_cache_for_tests,
)
from core.types import Address

USDT = Address.from_string("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9")
ROUTER = Address.from_string("0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24")
WALLET_ADDR = "0x000000000000000000000000000000000000dEaD"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_approved_cache_for_tests()
    yield
    reset_approved_cache_for_tests()


def _stub_wallet() -> MagicMock:
    w = MagicMock()
    w.address = WALLET_ADDR
    return w


def test_encode_approve_selector_and_args():
    """Calldata starts with ``approve(address,uint256)`` selector + 64-byte args."""
    data = _encode_approve(ROUTER, MAX_UINT256)
    assert data[:4].hex() == "095ea7b3"
    assert len(data) == 4 + 32 + 32
    # spender right-padded address (12 zero bytes + 20 address bytes).
    spender_word = data[4:36]
    assert spender_word.hex().endswith(ROUTER.lower[2:])
    # uint256 max == all 1s.
    amount_word = data[36:68]
    assert amount_word == b"\xff" * 32


def test_encode_approve_rejects_out_of_range():
    with pytest.raises(ValueError):
        _encode_approve(ROUTER, -1)
    with pytest.raises(ValueError):
        _encode_approve(ROUTER, MAX_UINT256 + 1)


def test_ensure_router_allowance_sufficient_skips_approve(monkeypatch: Any):
    """When chain already shows enough allowance, no approve tx is sent."""
    monkeypatch.setattr(erc20_approve, "get_allowance", lambda *a, **kw: MAX_UINT256)

    sent = []

    class _Builder:
        def __init__(self, *a: Any, **kw: Any) -> None:
            sent.append("constructed")

    monkeypatch.setattr(erc20_approve, "TransactionBuilder", _Builder)

    out = ensure_router_allowance(
        client=MagicMock(),
        wallet=_stub_wallet(),
        token=USDT,
        spender=ROUTER,
        min_amount=10**18,
    )
    assert out["approved"] is False
    assert out["reason"] == "sufficient"
    assert sent == [], "No transaction should be built when allowance is sufficient"


def test_ensure_router_allowance_cached_short_circuits(monkeypatch: Any):
    """A second call for the same (token, spender) skips the chain read."""
    calls = {"reads": 0}

    def _read(*a: Any, **kw: Any) -> int:
        calls["reads"] += 1
        return MAX_UINT256

    monkeypatch.setattr(erc20_approve, "get_allowance", _read)

    args = dict(
        client=MagicMock(),
        wallet=_stub_wallet(),
        token=USDT,
        spender=ROUTER,
        min_amount=10**18,
    )
    first = ensure_router_allowance(**args)
    second = ensure_router_allowance(**args)

    assert first["reason"] == "sufficient"
    assert second["reason"] == "cached"
    assert calls["reads"] == 1


def test_ensure_router_allowance_below_min_sends_approve(monkeypatch: Any):
    """When allowance < min_amount, build + send + wait approve(max_uint256)."""
    monkeypatch.setattr(erc20_approve, "get_allowance", lambda *a, **kw: 0)

    receipt = MagicMock()
    receipt.tx_hash = "0xabc123"
    builder = MagicMock()
    builder.to.return_value = builder
    builder.data.return_value = builder
    builder.with_gas_estimate.return_value = builder
    builder.with_gas_price.return_value = builder
    builder.send_and_wait.return_value = receipt

    constructed_with: list[Any] = []

    def _factory(client: Any, wallet: Any) -> Any:
        constructed_with.append((client, wallet))
        return builder

    monkeypatch.setattr(erc20_approve, "TransactionBuilder", _factory)

    out = ensure_router_allowance(
        client=MagicMock(),
        wallet=_stub_wallet(),
        token=USDT,
        spender=ROUTER,
        min_amount=10**18,
    )

    assert out["approved"] is True
    assert out["tx_hash"] == "0xabc123"
    assert out["current"] == MAX_UINT256
    builder.to.assert_called_once_with(USDT)
    builder.with_gas_estimate.assert_called_once()
    builder.with_gas_price.assert_called_once()
    builder.send_and_wait.assert_called_once()

    # Verify calldata is approve(router, max_uint256).
    calldata_arg = builder.data.call_args[0][0]
    assert calldata_arg[:4].hex() == "095ea7b3"
    assert calldata_arg[36:68] == b"\xff" * 32

    # Second call short-circuits via the cache (no extra build).
    out2 = ensure_router_allowance(
        client=MagicMock(),
        wallet=_stub_wallet(),
        token=USDT,
        spender=ROUTER,
        min_amount=10**18,
    )
    assert out2["reason"] == "cached"
    assert builder.send_and_wait.call_count == 1


def test_ensure_router_allowance_zero_min_amount_no_op(monkeypatch: Any):
    """``min_amount=0`` early-returns without touching the chain."""
    called = {"n": 0}

    def _read(*a: Any, **kw: Any) -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(erc20_approve, "get_allowance", _read)
    out = ensure_router_allowance(
        client=MagicMock(),
        wallet=_stub_wallet(),
        token=USDT,
        spender=ROUTER,
        min_amount=0,
    )
    assert out["approved"] is False
    assert out["reason"] == "zero_min"
    assert called["n"] == 0
