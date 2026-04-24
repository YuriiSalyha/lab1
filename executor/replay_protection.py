"""Prevents the same signal from executing twice within a TTL."""

from __future__ import annotations

import time

from strategy.signal import Signal

DEFAULT_REPLAY_TTL_S = 60.0


class ReplayProtection:
    """Tiny in-memory signal-id -> timestamp dedup set with TTL pruning."""

    def __init__(self, ttl_seconds: float = DEFAULT_REPLAY_TTL_S) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.executed: dict[str, float] = {}
        self.ttl = float(ttl_seconds)

    def is_duplicate(self, signal: Signal) -> bool:
        self._cleanup()
        return signal.signal_id in self.executed

    def mark_executed(self, signal: Signal) -> None:
        self.executed[signal.signal_id] = time.time()

    def clear(self) -> None:
        """Wipe all tracked signal ids."""
        self.executed.clear()

    def _cleanup(self) -> None:
        cutoff = time.time() - self.ttl
        # Rebuild to avoid mutating while iterating.
        self.executed = {k: v for k, v in self.executed.items() if v > cutoff}
