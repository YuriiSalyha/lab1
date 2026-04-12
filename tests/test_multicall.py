"""Tests for Multicall3 aggregate wrapper."""

from unittest.mock import MagicMock

from chain.multicall import MulticallCall, aggregate3


def test_aggregate3_empty_returns_empty() -> None:
    w3 = MagicMock()
    assert aggregate3(w3, []) == []


def test_aggregate3_decodes_results() -> None:
    w3 = MagicMock()
    contract = MagicMock()
    w3.eth.contract.return_value = contract
    contract.functions.aggregate3.return_value.call.return_value = [
        (True, b"\x01\x02"),
        (False, b""),
    ]
    t1 = "0x0000000000000000000000000000000000000001"
    t2 = "0x0000000000000000000000000000000000000002"
    out = aggregate3(
        w3,
        [
            MulticallCall(target=t1, data=b"ab", allow_failure=True),
            MulticallCall(target=t2, data=b"cd", allow_failure=True),
        ],
    )
    assert len(out) == 2
    assert out[0].success is True
    assert out[0].return_data == b"\x01\x02"
    assert out[1].success is False
