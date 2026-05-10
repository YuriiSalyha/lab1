"""Etherscan API v2 gas oracle (Gwei suggestions); see module docstring for limits."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
# Gas tracker returns suggested gas prices in **Gwei** (nano-ETH per gas unit).
# Unit facts (Ethereum convention):
#   1 Gwei = 1e-9 ETH = 0.000000001 ETH = 1e9 wei.
# So max fee in wei for ``gas_units`` at ``P`` Gwei/gas is: ``gas_units * P * 10**9``.
# This does not include Arbitrum's separate L1 calldata posting — callers apply a
# multiplier or use receipt-based fees when that matters.


def fetch_gas_oracle_proposed_gwei(
    api_key: str,
    chain_id: int,
    *,
    timeout_s: float = 10.0,
) -> Decimal | None:
    """Return ``ProposeGasPrice`` from Etherscan ``gasoracle`` as :class:`~decimal.Decimal` Gwei.

    Returns ``None`` on network/parse errors or non-OK API status.
    """
    key = (api_key or "").strip()
    if not key or chain_id <= 0:
        return None
    q = urllib.parse.urlencode(
        {
            "chainid": str(int(chain_id)),
            "module": "gastracker",
            "action": "gasoracle",
            "apikey": key,
        }
    )
    url = f"{ETHERSCAN_V2}?{q}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lab1-arb-bot"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("etherscan gasoracle fetch failed: %s", exc)
        return None
    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("etherscan gasoracle: invalid JSON")
        return None
    if str(payload.get("status")) != "1":
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    prop = result.get("ProposeGasPrice") or result.get("FastGasPrice") or result.get("SafeGasPrice")
    if prop is None:
        return None
    try:
        gwei = Decimal(str(prop))
    except Exception:
        return None
    return gwei if gwei > 0 else None


def oracle_l2_fee_wei_upper_bound(gwei: Decimal, gas_units: int) -> int:
    """Upper-bound L2 execution fee in wei from gas limit × gas price (Gwei).

    ``gwei`` is the numeric gas price **in Gwei per gas unit** (as returned by
    Etherscan gasoracle). With ``1 Gwei = 10**9 wei`` and ``1 Gwei = 10**-9 ETH``:

        fee_wei = gas_units × gwei × 10**9
    """
    if gas_units <= 0 or gwei <= 0:
        return 0
    # Decimal -> int without float: gas_units * gwei * 10**9
    d = Decimal(gas_units) * gwei * Decimal(10**9)
    return int(d.to_integral_value(rounding="ROUND_CEILING"))
