"""Tests for :mod:`pricing.dynamic_gas_usd`."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from pricing.dynamic_gas_usd import GasUsdEstimator


def test_estimator_no_oracle_returns_floor() -> None:
    est = GasUsdEstimator(
        floor_usd=Decimal("0.02"),
        etherscan_refresh_sec=60.0,
        oracle_gas_units=100_000,
        l1_mult=Decimal("2"),
        etherscan_api_key=None,
        chain_id=None,
    )
    assert est.current_usd(eth_usd=Decimal("3000")) == Decimal("0.02")


def test_estimator_uses_oracle_when_configured() -> None:
    est = GasUsdEstimator(
        floor_usd=Decimal("0.05"),
        etherscan_refresh_sec=0.0,
        oracle_gas_units=100_000,
        l1_mult=Decimal("1"),
        etherscan_api_key="k",
        chain_id=42161,
        clamp_oracle_to_floor=False,
    )

    def _fake_fetch(*a: object, **kw: object) -> Decimal:
        return Decimal("1")  # 1 Gwei

    with patch("pricing.dynamic_gas_usd.fetch_gas_oracle_proposed_gwei", _fake_fetch):
        out = est.current_usd(eth_usd=Decimal("2000"), now_mono=100.0)
    assert out == Decimal("0.2")


def test_estimator_clamp_raises_oracle_to_user_floor() -> None:
    est = GasUsdEstimator(
        floor_usd=Decimal("0.15"),
        etherscan_refresh_sec=0.0,
        oracle_gas_units=100_000,
        l1_mult=Decimal("1"),
        etherscan_api_key="k",
        chain_id=42161,
        clamp_oracle_to_floor=True,
    )

    def _fake_fetch(*a: object, **kw: object) -> Decimal:
        return Decimal("0.1")  # small enough that oracle USD < floor at eth=2000

    with patch("pricing.dynamic_gas_usd.fetch_gas_oracle_proposed_gwei", _fake_fetch):
        out = est.current_usd(eth_usd=Decimal("2000"), now_mono=100.0)
    assert out == Decimal("0.15")
