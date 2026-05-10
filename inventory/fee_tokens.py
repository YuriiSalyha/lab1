"""Env-driven symbols treated as chain / fee currency (e.g. native ETH on Arbitrum)."""

from __future__ import annotations

import os
from typing import Sequence

ENV_ARB_INVENTORY_FEE_TOKENS = "ARB_INVENTORY_FEE_TOKENS"


def normalize_fee_token_list(symbols: Sequence[str] | None) -> tuple[str, ...]:
    """Uppercase, strip, drop empties, dedupe preserving order."""
    if not symbols:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols:
        s = str(raw).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return tuple(out)


def parse_fee_tokens_from_env(env_var: str | None = None) -> tuple[str, ...]:
    """Parse comma-separated fee-token symbols from ``os.environ``."""
    key = env_var or ENV_ARB_INVENTORY_FEE_TOKENS
    raw = os.getenv(key, "").strip()
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    return normalize_fee_token_list(parts)
