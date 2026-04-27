"""Tests for :mod:`executor.webhook_alerts`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from executor.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from executor.webhook_alerts import (
    WebhookDeliveryConfig,
    circuit_breaker_payload,
    make_circuit_breaker_webhook_hook,
    post_json_webhook,
)


def test_circuit_breaker_payload_shape():
    cb = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=2, window_seconds=60, cooldown_seconds=30),
    )
    cb.record_failure()
    body = circuit_breaker_payload(cb)
    assert body["event"] == "circuit_breaker_tripped"
    assert body["failure_count"] == 1
    assert body["failure_threshold"] == 2


@patch("executor.webhook_alerts.urllib.request.urlopen")
def test_post_json_webhook_sends_json(mock_urlopen: MagicMock) -> None:
    mock_cm = MagicMock()
    mock_cm.read.return_value = b"{}"
    mock_cm.__enter__.return_value = mock_cm
    mock_urlopen.return_value = mock_cm

    post_json_webhook("https://example.test/hook", {"hello": "world"}, timeout_seconds=2.0)
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.get_full_url() == "https://example.test/hook"
    assert req.data == b'{"hello": "world"}'


def test_webhook_hook_does_not_double_fire_on_re_trip():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, **kwargs: object) -> None:
        posted.append(dict(payload))

    cb = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=1, window_seconds=300, cooldown_seconds=600),
        on_trip=make_circuit_breaker_webhook_hook(
            WebhookDeliveryConfig(url="https://example.test/x", timeout_seconds=1.0),
        ),
    )

    with patch("executor.webhook_alerts.post_json_webhook", side_effect=fake_post):
        cb.record_failure()
        assert cb.is_open()
        assert len(posted) == 1
        # Trip again while open — must not notify again.
        cb.trip()
        cb.trip()
        assert len(posted) == 1


def test_chain_trip_hooks_one_failure_does_not_block_other():
    a_calls: list[int] = []
    b_calls: list[int] = []

    def a(_cb: object) -> None:
        a_calls.append(1)
        raise RuntimeError("a")

    def b(_cb: object) -> None:
        b_calls.append(1)

    from executor.webhook_alerts import chain_trip_hooks

    h = chain_trip_hooks(a, b)
    cb = CircuitBreaker(on_trip=h)
    cb.trip()
    assert a_calls == [1]
    assert b_calls == [1]
