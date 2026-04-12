"""Data types for off-chain arbitrage scanning."""

from __future__ import annotations

from dataclasses import dataclass

from core.types import Token
from pricing.liquidity_pool import LiquidityPoolQuote


@dataclass(frozen=True, slots=True)
class ArbitrageOpportunity:
    """Profitable round-trip in one start token (raw integer units)."""

    pools: tuple[LiquidityPoolQuote, ...]
    path: tuple[Token, ...]
    amount_in: int
    amount_out: int
    profit_raw: int
    profit_bps: int
    gas_estimate: int
    gas_cost_start_token: int
    profit_net: int
