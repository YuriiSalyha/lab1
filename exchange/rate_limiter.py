"""
Sliding-window request weight limiter for Binance REST–style weight budgets.

Blocks (sleeps) until a request of a given weight can be added without exceeding
``max_weight`` summed over the last ``window_sec`` seconds.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class WeightRateLimiter:
    """
    Track cumulative request weight in a sliding time window.

    Before each call, :meth:`acquire` sleeps if needed so that the sum of weights
    in the window does not exceed ``max_weight``.
    """

    def __init__(self, max_weight: int, window_sec: float = 60.0) -> None:
        if max_weight < 1:
            raise ValueError("max_weight must be >= 1")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self.max_weight = max_weight
        self.window_sec = window_sec
        self._events: deque[tuple[float, int]] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def _total(self) -> int:
        return sum(w for _, w in self._events)

    def acquire(self, weight: int) -> None:
        """Wait until ``weight`` can be spent without exceeding the budget."""
        if weight < 0:
            raise ValueError("weight must be non-negative")
        if weight == 0:
            return
        if weight > self.max_weight:
            raise ValueError(
                f"request weight {weight} exceeds limiter max_weight {self.max_weight}"
            )

        while True:
            now = time.monotonic()
            self._prune(now)
            total = self._total()
            if total + weight <= self.max_weight:
                self._events.append((now, weight))
                return

            oldest_t = self._events[0][0]
            sleep_for = oldest_t + self.window_sec - now
            if sleep_for <= 0:
                self._events.popleft()
                continue

            logger.debug(
                "rate limiter: window full (total=%s + request=%s > max=%s), sleeping %.3fs",
                total,
                weight,
                self.max_weight,
                sleep_for,
            )
            time.sleep(sleep_for)
