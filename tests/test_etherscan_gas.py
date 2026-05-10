"""Tests for :mod:`pricing.etherscan_gas`."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from pricing.etherscan_gas import fetch_gas_oracle_proposed_gwei, oracle_l2_fee_wei_upper_bound


def test_oracle_l2_fee_wei_upper_bound() -> None:
    wei = oracle_l2_fee_wei_upper_bound(Decimal("0.02"), 200_000)
    assert wei == int(Decimal("200_000") * Decimal("0.02") * Decimal(10**9))


def test_fetch_gas_oracle_success() -> None:
    payload = json.dumps(
        {
            "status": "1",
            "message": "OK",
            "result": {
                "ProposeGasPrice": "0.02001",
                "SafeGasPrice": "0.02",
                "FastGasPrice": "0.021",
            },
        }
    ).encode()

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    with patch("pricing.etherscan_gas.urllib.request.urlopen", return_value=_Resp()):
        g = fetch_gas_oracle_proposed_gwei("k", 42161)
    assert g == Decimal("0.02001")


def test_fetch_gas_oracle_bad_status() -> None:
    payload = json.dumps({"status": "0", "result": "Max rate limit"}).encode()

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    with patch("pricing.etherscan_gas.urllib.request.urlopen", return_value=_Resp()):
        assert fetch_gas_oracle_proposed_gwei("k", 42161) is None
