"""Tests for :mod:`chain.ws_env`."""

import pytest

from chain.ws_env import resolve_websocket_url


@pytest.fixture(autouse=True)
def _no_dotenv_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid picking up a developer ``.env`` during URL resolution tests."""
    monkeypatch.setattr("chain.ws_env.load_dotenv", lambda *a, **k: None)


def test_resolve_prefers_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAINNET_WS", "wss://from-env-should-not-win")
    assert resolve_websocket_url("wss://from-cli") == "wss://from-cli"


def test_resolve_first_env_key_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAINNET_WS", raising=False)
    monkeypatch.setenv("ETH_MAINNET_WS", "wss://eth-mainnet-ws")
    monkeypatch.delenv("RPC_WS", raising=False)
    monkeypatch.delenv("ALCHEMY_WS", raising=False)
    monkeypatch.delenv("WS_URL", raising=False)
    assert resolve_websocket_url(None) == "wss://eth-mainnet-ws"


def test_resolve_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "MAINNET_WS",
        "ETH_MAINNET_WS",
        "RPC_WS",
        "ALCHEMY_WS",
        "WS_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="Set one of"):
        resolve_websocket_url(None)
