"""Fee model used by the signal generator and the executor.

All arithmetic is in :class:`~decimal.Decimal`. Callers may pass ``float``/``int``
inputs — they are coerced via :func:`strategy.signal.to_decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from strategy.signal import to_decimal

BPS_DENOM = Decimal("10000")
DEFAULT_CEX_TAKER_BPS = Decimal("10")
DEFAULT_DEX_SWAP_BPS = Decimal("30")
DEFAULT_GAS_COST_USD = Decimal("5")


@dataclass
class FeeStructure:
    """Static fee assumptions for CEX taker + DEX swap + gas."""

    cex_taker_bps: Decimal = DEFAULT_CEX_TAKER_BPS
    dex_swap_bps: Decimal = DEFAULT_DEX_SWAP_BPS
    gas_cost_usd: Decimal = DEFAULT_GAS_COST_USD

    def __post_init__(self) -> None:
        self.cex_taker_bps = to_decimal(self.cex_taker_bps)
        self.dex_swap_bps = to_decimal(self.dex_swap_bps)
        self.gas_cost_usd = to_decimal(self.gas_cost_usd)
        if self.cex_taker_bps < 0 or self.dex_swap_bps < 0 or self.gas_cost_usd < 0:
            raise ValueError("fee fields must be non-negative")

    @staticmethod
    def _positive(name: str, value: Any) -> Decimal:
        d = to_decimal(value)
        if d <= 0:
            raise ValueError(f"{name} must be positive, got {value!r}")
        return d

    def gas_bps(self, trade_value_usd: Any) -> Decimal:
        """Gas cost expressed as bps of trade value."""
        tv = self._positive("trade_value_usd", trade_value_usd)
        return self.gas_cost_usd / tv * BPS_DENOM

    def total_fee_bps(self, trade_value_usd: Any) -> Decimal:
        """CEX taker + DEX swap + amortized gas, all in bps."""
        return self.cex_taker_bps + self.dex_swap_bps + self.gas_bps(trade_value_usd)

    def breakeven_spread_bps(self, trade_value_usd: Any) -> Decimal:
        """Spread required (in bps) to offset all fees at ``trade_value_usd``."""
        return self.total_fee_bps(trade_value_usd)

    def total_fee_usd(self, trade_value_usd: Any) -> Decimal:
        """Total cost in USD for a notional of ``trade_value_usd``."""
        tv = self._positive("trade_value_usd", trade_value_usd)
        return self.total_fee_bps(tv) / BPS_DENOM * tv

    def net_profit_usd(self, spread_bps: Any, trade_value_usd: Any) -> Decimal:
        """Net USD profit given gross spread (bps) and notional (USD)."""
        tv = self._positive("trade_value_usd", trade_value_usd)
        spread = to_decimal(spread_bps)
        gross = spread / BPS_DENOM * tv
        return gross - self.total_fee_usd(tv)
