"""Gas USD for ``FeeStructure`` from Etherscan gasoracle only (no on-chain receipt history)."""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Optional

from pricing.etherscan_gas import fetch_gas_oracle_proposed_gwei, oracle_l2_fee_wei_upper_bound
from strategy.fees import DEFAULT_GAS_COST_USD
from strategy.signal import to_decimal


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return to_decimal(raw)
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class GasUsdEstimator:
    """USD gas per DEX swap from **Etherscan gasoracle** × gas units × ETH/USD × L1 mult.

    Refreshes on ``ARB_GAS_ETHERSCAN_REFRESH_SEC``.

    When ``ARB_GAS_COST_USD`` is **unset**, a positive oracle estimate is used as-is
    (not raised to the package default floor), so Arbitrum-sized fees match reality.

    When ``ARB_GAS_COST_USD`` **is** set, it acts as a **minimum** model gas:
    ``max(ARB_GAS_COST_USD, oracle_usd)`` whenever the oracle returns a value.

    If the oracle is unavailable, ``floor_usd`` (initial :class:`~strategy.fees.FeeStructure`
    gas, from env or :data:`~strategy.fees.DEFAULT_GAS_COST_USD`) is used.
    """

    def __init__(
        self,
        *,
        floor_usd: Decimal,
        etherscan_refresh_sec: float,
        oracle_gas_units: int,
        l1_mult: Decimal,
        etherscan_api_key: Optional[str],
        chain_id: Optional[int],
        clamp_oracle_to_floor: bool = False,
    ) -> None:
        self._floor = floor_usd if floor_usd >= 0 else DEFAULT_GAS_COST_USD
        self._clamp_oracle_to_floor = clamp_oracle_to_floor
        self._etherscan_refresh_sec = float(etherscan_refresh_sec)
        self._oracle_gas_units = max(1, int(oracle_gas_units))
        self._l1_mult = l1_mult if l1_mult > 0 else Decimal("1")
        self._etherscan_api_key = (etherscan_api_key or "").strip() or None
        self._chain_id = int(chain_id) if chain_id and int(chain_id) > 0 else None
        self._last_oracle_mono: float = 0.0
        self._cached_oracle_usd: Optional[Decimal] = None

    @classmethod
    def from_env(
        cls, floor_usd: Decimal, *, clamp_oracle_to_floor: bool = False
    ) -> GasUsdEstimator:
        """Build from ``ARB_GAS_*``, ``ETHERSCAN_API_KEY``, ``DEX_EXPECTED_CHAIN_ID``."""
        cid_raw = (os.getenv("DEX_EXPECTED_CHAIN_ID", "") or "").strip()
        chain_id: Optional[int] = None
        if cid_raw.isdigit():
            chain_id = int(cid_raw)
        return cls(
            floor_usd=floor_usd if floor_usd >= 0 else DEFAULT_GAS_COST_USD,
            etherscan_refresh_sec=_float_env("ARB_GAS_ETHERSCAN_REFRESH_SEC", 60.0),
            oracle_gas_units=_int_env("ARB_GAS_ORACLE_GAS_UNITS", 180_000),
            l1_mult=_decimal_env("ARB_GAS_ETHERSCAN_L1_MULT", Decimal("2.5")),
            etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
            chain_id=chain_id,
            clamp_oracle_to_floor=clamp_oracle_to_floor,
        )

    def current_usd(
        self, *, eth_usd: Optional[Decimal], now_mono: Optional[float] = None
    ) -> Decimal:
        oracle_usd = self._maybe_oracle_usd(eth_usd=eth_usd, now_mono=now_mono)
        if oracle_usd is not None and oracle_usd > 0:
            if self._clamp_oracle_to_floor:
                return max(self._floor, oracle_usd)
            return oracle_usd
        return self._floor

    def _maybe_oracle_usd(
        self,
        *,
        eth_usd: Optional[Decimal],
        now_mono: Optional[float],
    ) -> Optional[Decimal]:
        if not self._etherscan_api_key or self._chain_id is None or eth_usd is None or eth_usd <= 0:
            return self._cached_oracle_usd
        t = float(now_mono if now_mono is not None else time.monotonic())
        if (
            self._cached_oracle_usd is not None
            and (t - self._last_oracle_mono) < self._etherscan_refresh_sec
        ):
            return self._cached_oracle_usd
        gwei = fetch_gas_oracle_proposed_gwei(self._etherscan_api_key, self._chain_id)
        self._last_oracle_mono = t
        if gwei is None:
            return self._cached_oracle_usd
        wei = oracle_l2_fee_wei_upper_bound(gwei, self._oracle_gas_units)
        if wei <= 0:
            return self._cached_oracle_usd
        l2_eth = Decimal(wei) / Decimal(10**18)
        usd = l2_eth * eth_usd * self._l1_mult
        self._cached_oracle_usd = usd
        return usd
