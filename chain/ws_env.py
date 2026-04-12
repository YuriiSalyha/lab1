"""Resolve WebSocket JSON-RPC URL from CLI or environment."""

from __future__ import annotations

import os

from dotenv import load_dotenv

_DEFAULT_ENV_KEYS = (
    "MAINNET_WS",
    "ETH_MAINNET_WS",
    "RPC_WS",
    "ALCHEMY_WS",
    "WS_URL",
)


def resolve_websocket_url(
    cli_ws: str | None,
    *,
    env_keys: tuple[str, ...] = _DEFAULT_ENV_KEYS,
) -> str:
    """
    Return ``wss://`` URL from *cli_ws* if set, else first non-empty *env_keys* value.

    Calls :func:`dotenv.load_dotenv` so a local ``.env`` is picked up.
    """
    load_dotenv()
    if cli_ws and cli_ws.strip():
        return cli_ws.strip()
    for key in env_keys:
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise ValueError(
        "Set one of " + ", ".join(env_keys) + " or pass a non-empty WebSocket URL (wss://…)."
    )
