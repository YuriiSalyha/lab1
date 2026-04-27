"""Best-effort HTTP webhook notifications (stdlib only)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_TIMEOUT_S = 5.0
DEFAULT_WEBHOOK_METHOD = "POST"
WEBHOOK_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
WEBHOOK_USER_AGENT = "lab1-circuit-breaker/1.0"


@dataclass(frozen=True)
class WebhookDeliveryConfig:
    url: str
    timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_S
    extra_headers: Optional[dict[str, str]] = None


def post_json_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_S,
    extra_headers: Optional[dict[str, str]] = None,
) -> None:
    """POST JSON to ``url``; logs and swallows transport errors (alert path must not raise)."""
    body = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method=DEFAULT_WEBHOOK_METHOD,
        headers={
            "Content-Type": WEBHOOK_JSON_CONTENT_TYPE,
            "User-Agent": WEBHOOK_USER_AGENT,
            **(extra_headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            _ = resp.read(256)
    except urllib.error.HTTPError as e:
        logger.warning("webhook HTTP error %s: %s", e.code, e.reason)
    except urllib.error.URLError as e:
        logger.warning("webhook URL error: %s", e.reason)
    except Exception as e:
        logger.warning("webhook failed: %s", e)


def circuit_breaker_payload(cb: Any) -> dict[str, Any]:
    """Serializable body for a circuit-breaker trip notification."""
    return {
        "event": "circuit_breaker_tripped",
        "failure_count": cb.current_failures(),
        "failure_threshold": cb.failure_threshold,
        "cooldown_seconds": cb.config.cooldown_seconds,
        "window_seconds": cb.config.window_seconds,
        "time_until_reset_s": cb.time_until_reset(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def make_circuit_breaker_webhook_hook(
    cfg: WebhookDeliveryConfig,
) -> Callable[[Any], None]:
    """Return a synchronous hook suitable for :class:`~executor.circuit_breaker.CircuitBreaker`."""

    def _hook(cb: Any) -> None:
        post_json_webhook(
            cfg.url,
            circuit_breaker_payload(cb),
            timeout_seconds=cfg.timeout_seconds,
            extra_headers=cfg.extra_headers,
        )

    return _hook


def chain_trip_hooks(*hooks: Callable[[Any], None]) -> Callable[[Any], None]:
    """Invoke multiple trip hooks; each error is logged and does not block others."""

    def _combined(cb: Any) -> None:
        for h in hooks:
            try:
                h(cb)
            except Exception as e:
                logger.exception("trip hook failed: %s", e)

    return _combined
