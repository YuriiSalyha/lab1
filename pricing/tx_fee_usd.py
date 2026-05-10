"""Parse transaction receipts into fee (wei) for analysis helpers."""

from __future__ import annotations

from typing import Any, Mapping


def _rpc_int(value: Any) -> int:
    """Coerce Web3 / JSON-RPC hex or int fields to ``int``."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def transaction_fee_wei_from_receipt(receipt: Mapping[str, Any]) -> int:
    """Best-effort total execution fee in wei from a mined receipt.

    Uses ``gasUsed * effectiveGasPrice`` (EIP-1559). On some chains (including
    several Arbitrum RPC providers), extra keys such as ``l1Fee`` may appear; if
    present they are added.
    """
    gas_used = _rpc_int(receipt.get("gasUsed"))
    egp = receipt.get("effectiveGasPrice")
    if egp is None:
        egp = receipt.get("gasPrice")
    l2 = gas_used * _rpc_int(egp)
    extra = 0
    for key in ("l1Fee", "L1Fee", "l1_fee"):
        if key in receipt and receipt[key] not in (None, "0x", "0x0", 0, "0"):
            extra += _rpc_int(receipt[key])
    return l2 + extra
