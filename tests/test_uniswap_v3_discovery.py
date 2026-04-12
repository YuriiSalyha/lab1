"""Tests for :mod:`pricing.uniswap_v3_discovery`."""

from unittest.mock import MagicMock

from pricing.uniswap_v3_discovery import get_pool_address, pools_for_pair


def test_get_pool_address_none_for_zero() -> None:
    w3 = MagicMock()
    c = MagicMock()
    w3.eth.contract.return_value = c
    c.functions.getPool.return_value.call.return_value = (
        "0x0000000000000000000000000000000000000000"
    )
    t0 = "0x1111111111111111111111111111111111111111"
    t1 = "0x2222222222222222222222222222222222222222"
    assert get_pool_address(w3, t0, t1, 3000) is None


def test_pools_for_pair_collects_nonzero() -> None:
    w3 = MagicMock()
    c = MagicMock()
    w3.eth.contract.return_value = c

    c.functions.getPool.return_value.call.side_effect = [
        "0x0000000000000000000000000000000000000000",
        "0x" + "aa" * 20,
        "0x0000000000000000000000000000000000000000",
    ]
    out = pools_for_pair(
        w3,
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        fee_tiers=(500, 3000, 10000),
    )
    assert len(out) == 1
    assert out[0][0] == 3000
    assert out[0][1].lower() == "0x" + "aa" * 20
