"""
Optional Prometheus metrics (``pip install lab1[metrics]``).

Uses lazy import so core installs stay free of ``prometheus_client``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_METRICS_NAMESPACE = "lab1"
DEFAULT_METRICS_SUBSYSTEM = "arb"


class PrometheusMetrics:
    """Counters/histograms for executor outcomes; no-op if import or bind fails."""

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_METRICS_NAMESPACE,
        subsystem: str = DEFAULT_METRICS_SUBSYSTEM,
    ) -> None:
        self._exec_counter: Any = None
        self._trip_counter: Any = None
        self._dur_hist: Any = None
        self._enabled = False
        try:
            from prometheus_client import Counter, Histogram

            self._exec_counter = Counter(
                "executions_total",
                "Executor runs by terminal state",
                ("result",),
                namespace=namespace,
                subsystem=subsystem,
            )
            self._trip_counter = Counter(
                "circuit_breaker_trips_total",
                "Circuit breaker opened",
                namespace=namespace,
                subsystem=subsystem,
            )
            self._dur_hist = Histogram(
                "execution_duration_seconds",
                "Wall time for Executor.execute",
                namespace=namespace,
                subsystem=subsystem,
            )
            self._enabled = True
        except Exception as e:
            logger.warning("Prometheus metrics disabled: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_execution(self, result_name: str, duration_s: float) -> None:
        if not self._enabled or self._exec_counter is None or self._dur_hist is None:
            return
        self._exec_counter.labels(result=result_name).inc()
        self._dur_hist.observe(max(0.0, duration_s))

    def record_circuit_trip(self) -> None:
        if not self._enabled or self._trip_counter is None:
            return
        self._trip_counter.inc()


def try_start_metrics_server(port: int) -> Optional[Any]:
    """
    Bind ``prometheus_client`` HTTP /metrics on ``port``.

    Returns the server object or ``None`` if unavailable or port invalid.
    """
    if port <= 0 or port > 65535:
        return None
    try:
        from prometheus_client import start_http_server

        return start_http_server(port)
    except Exception as e:
        logger.warning("Prometheus HTTP server not started: %s", e)
        return None
