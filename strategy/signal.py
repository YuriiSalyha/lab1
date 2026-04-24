"""Signal dataclass + helpers for arbitrage opportunity representation.

All monetary, spread, size, and score fields are :class:`~decimal.Decimal` so
every downstream calculation stays exact. Timestamps remain ``float`` seconds
(they are durations, not money, and matching ``time.time()``).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

DEFAULT_SIGNAL_TTL_S = 5.0
SIGNAL_ID_HEX_LEN = 8
SCORE_MIN = Decimal("0")
SCORE_MAX = Decimal("100")


class Direction(str, Enum):
    """Arbitrage direction: which venue we buy on."""

    BUY_CEX_SELL_DEX = "buy_cex_sell_dex"
    BUY_DEX_SELL_CEX = "buy_dex_sell_cex"


def to_decimal(value: Any) -> Decimal:
    """Coerce any numeric input to :class:`Decimal` without float-artefacts.

    ``Decimal(str(x))`` avoids binary-float surprises (``Decimal(0.1)`` → noise).
    """
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


@dataclass
class Signal:
    """A validated arbitrage opportunity ready for execution."""

    signal_id: str
    pair: str
    direction: Direction

    cex_price: Decimal
    dex_price: Decimal
    spread_bps: Decimal
    size: Decimal

    expected_gross_pnl: Decimal
    expected_fees: Decimal
    expected_net_pnl: Decimal

    score: Decimal
    timestamp: float
    expiry: float

    inventory_ok: bool
    within_limits: bool

    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cex_price = to_decimal(self.cex_price)
        self.dex_price = to_decimal(self.dex_price)
        self.spread_bps = to_decimal(self.spread_bps)
        self.size = to_decimal(self.size)
        self.expected_gross_pnl = to_decimal(self.expected_gross_pnl)
        self.expected_fees = to_decimal(self.expected_fees)
        self.expected_net_pnl = to_decimal(self.expected_net_pnl)
        self.score = to_decimal(self.score)

        if self.size <= 0:
            raise ValueError(f"Signal size must be positive, got {self.size}")
        if self.cex_price <= 0 or self.dex_price <= 0:
            raise ValueError(
                f"Signal prices must be positive, got cex={self.cex_price} dex={self.dex_price}",
            )
        if self.score < SCORE_MIN:
            self.score = SCORE_MIN
        elif self.score > SCORE_MAX:
            self.score = SCORE_MAX
        if self.expiry < self.timestamp:
            raise ValueError("Signal expiry must be >= timestamp")
        if "/" not in self.pair:
            raise ValueError(f"pair must be unified form BASE/QUOTE, got {self.pair!r}")

    @classmethod
    def create(cls, pair: str, direction: Direction, **kwargs: Any) -> "Signal":
        """Build a ``Signal`` with auto-generated id and current timestamp."""
        base = pair.replace("/", "")
        return cls(
            signal_id=f"{base}_{uuid.uuid4().hex[:SIGNAL_ID_HEX_LEN]}",
            pair=pair,
            direction=direction,
            timestamp=kwargs.pop("timestamp", time.time()),
            **kwargs,
        )

    def is_valid(self) -> bool:
        """True if signal is still fresh, profitable, and respects limits."""
        return len(self.invalidity_reasons()) == 0

    def invalidity_reasons(self) -> list[str]:
        """Human-readable reasons :meth:`is_valid` is false (empty if valid)."""
        reasons: list[str] = []
        if time.time() >= self.expiry:
            reasons.append("expired")
        if not self.inventory_ok:
            reasons.append("inventory")
        if not self.within_limits:
            reasons.append("notional_limit")
        if self.expected_net_pnl <= Decimal("0"):
            reasons.append("non_positive_expected_net_pnl")
        if self.score <= SCORE_MIN:
            reasons.append("score_not_above_minimum")
        return reasons

    def age_seconds(self) -> float:
        """How many seconds ago the signal was emitted."""
        return time.time() - self.timestamp

    def ttl_seconds(self) -> float:
        """Declared lifetime in seconds (expiry - timestamp)."""
        return self.expiry - self.timestamp
