"""Tests for :mod:`pricing.tx_fee_usd`."""

from __future__ import annotations

from pricing.tx_fee_usd import transaction_fee_wei_from_receipt


def test_transaction_fee_wei_eip1559_only() -> None:
    r = {
        "gasUsed": "0x5208",  # 21000
        "effectiveGasPrice": "0x3b9aca00",  # 1 gwei
    }
    assert transaction_fee_wei_from_receipt(r) == 21000 * 10**9


def test_transaction_fee_wei_adds_l1fee_when_present() -> None:
    r = {
        "gasUsed": 100_000,
        "effectiveGasPrice": 2 * 10**9,
        "l1Fee": hex(5 * 10**15),
    }
    l2 = 100_000 * 2 * 10**9
    assert transaction_fee_wei_from_receipt(r) == l2 + 5 * 10**15


def test_transaction_fee_wei_falls_back_gas_price() -> None:
    r = {"gasUsed": "0x64", "gasPrice": "0xa"}
    assert transaction_fee_wei_from_receipt(r) == 100 * 10
