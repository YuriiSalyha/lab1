"""Circuit breaker that trips after N failures in a rolling window.

Once tripped the breaker reports ``is_open() == True`` for ``cooldown_seconds``
and then auto-resets. Successes are *leaky*: each success removes the oldest
failure so a stable run will eventually clear stale failure spikes without
forcing a full cooldown.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

OnTripHook = Callable[[Any], None]

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_WINDOW_S = 300.0
DEFAULT_COOLDOWN_S = 600.0


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    window_seconds: float = DEFAULT_WINDOW_S
    cooldown_seconds: float = DEFAULT_COOLDOWN_S

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.window_seconds <= 0 or self.cooldown_seconds <= 0:
            raise ValueError("window_seconds and cooldown_seconds must be positive")


class CircuitBreaker:
    """Rolling-window failure circuit breaker."""

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        *,
        on_trip: Optional[OnTripHook] = None,
    ) -> None:
        self.config = config or CircuitBreakerConfig()
        self.failures: list[float] = []
        self.tripped_at: Optional[float] = None
        self._on_trip = on_trip

    @property
    def failure_threshold(self) -> int:
        return self.config.failure_threshold

    def record_failure(self) -> None:
        now = time.time()
        self._prune(now)
        self.failures.append(now)
        if len(self.failures) >= self.config.failure_threshold:
            self.trip()

    def record_success(self) -> None:
        """Leaky success: drops the oldest failure from the window."""
        self._prune(time.time())
        if self.failures:
            self.failures.pop(0)

    def trip(self) -> None:
        """Mark breaker as open; ``is_open()`` returns True until cooldown elapses."""
        if self.tripped_at is None:
            self.tripped_at = time.time()
            logger.critical(
                "CIRCUIT BREAKER TRIPPED (failures=%d, cooldown=%.0fs)",
                len(self.failures),
                self.config.cooldown_seconds,
            )
            if self._on_trip is not None:
                try:
                    self._on_trip(self)
                except Exception:
                    logger.exception("on_trip hook raised")

    def is_open(self) -> bool:
        if self.tripped_at is None:
            return False
        if time.time() - self.tripped_at > self.config.cooldown_seconds:
            self.tripped_at = None
            self.failures.clear()
            return False
        return True

    def current_failures(self) -> int:
        """Count of failures still inside the rolling window."""
        self._prune(time.time())
        return len(self.failures)

    def time_until_reset(self) -> float:
        if self.tripped_at is None:
            return 0.0
        remaining = self.config.cooldown_seconds - (time.time() - self.tripped_at)
        return max(0.0, remaining)

    def _prune(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        self.failures = [t for t in self.failures if t > cutoff]
