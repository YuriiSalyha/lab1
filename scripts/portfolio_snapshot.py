#!/usr/bin/env python3
"""Live ``InventoryTracker`` snapshot: Binance (``BINANCE_CONFIG``) + wallet ETH."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chain.client import ChainClient
from config.config import BINANCE_CONFIG
from core.types import Address
from core.wallet import WalletManager
from exchange.client import ExchangeClient
from inventory.tracker import InventoryTracker, Venue


def _rpc(cli: str | None) -> str | None:
    if cli and cli.strip():
        return cli.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT", "SEPOLIA_RPC"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


def _json_safe(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Portfolio snapshot: Binance testnet + wallet native ETH",
    )
    p.add_argument(
        "--rpc",
        default=None,
        help=(
            "HTTP RPC for wallet ETH; env: MAINNET_RPC, ETH_MAINNET_RPC, "
            "RPC_ENDPOINT, or SEPOLIA_RPC"
        ),
    )
    args = p.parse_args(argv)

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    notes: list[str] = []

    xc = ExchangeClient(BINANCE_CONFIG)
    try:
        tracker.update_from_cex(Venue.BINANCE, xc.fetch_balance())
    except Exception as e:
        notes.append(f"binance_balance_failed: {e}")

    rpc = _rpc(args.rpc)
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if pk:
        if not rpc:
            notes.append("wallet_skipped: set --rpc or MAINNET_RPC / RPC_ENDPOINT / SEPOLIA_RPC")
        else:
            try:
                wm = WalletManager.from_env("PRIVATE_KEY")
                chain = ChainClient([rpc])
                bal = chain.get_balance(Address.from_string(wm.address))
                tracker.update_from_wallet(Venue.WALLET, {"ETH": bal.human})
            except Exception as e:
                notes.append(f"wallet_balance_failed: {e}")
    else:
        notes.append("wallet_skipped: PRIVATE_KEY not set")

    snap = tracker.snapshot()
    out = _json_safe(snap)
    if notes:
        out["_notes"] = notes
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
