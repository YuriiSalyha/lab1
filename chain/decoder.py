"""Transaction calldata decoder and event log parser.

Identifies and decodes common DeFi function calls (ERC-20, Uniswap V2/V3)
and parses well-known event logs (Transfer, Swap, Sync).
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Mapping
from typing import Any, Optional

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

from chain.errors import InvalidParameterError
from chain.uniswap_v2_router import UNISWAP_V2_ROUTER_SWAP_ENTRIES
from chain.validation import (
    validate_calldata_input,
    validate_log_dict,
    validate_logs_list,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known function selectors  (4-byte hex → metadata)
# ---------------------------------------------------------------------------

_FUNCTION_SELECTORS: dict[str, dict[str, Any]] = {
    # ── ERC-20 ────────────────────────────────────────────────────────────
    "a9059cbb": {
        "name": "transfer",
        "types": ["address", "uint256"],
        "param_names": ["to", "value"],
    },
    "095ea7b3": {
        "name": "approve",
        "types": ["address", "uint256"],
        "param_names": ["spender", "value"],
    },
    "23b872dd": {
        "name": "transferFrom",
        "types": ["address", "address", "uint256"],
        "param_names": ["from", "to", "value"],
    },
    "70a08231": {
        "name": "balanceOf",
        "types": ["address"],
        "param_names": ["account"],
    },
    "18160ddd": {
        "name": "totalSupply",
        "types": [],
        "param_names": [],
    },
    # ── Uniswap V2 Router (swap methods — see :mod:`chain.uniswap_v2_router`) ─
    **UNISWAP_V2_ROUTER_SWAP_ENTRIES,
    "e8e33700": {
        "name": "addLiquidity",
        "types": [
            "address",
            "address",
            "uint256",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "uint256",
        ],
        "param_names": [
            "tokenA",
            "tokenB",
            "amountADesired",
            "amountBDesired",
            "amountAMin",
            "amountBMin",
            "to",
            "deadline",
        ],
    },
    "f305d719": {
        "name": "addLiquidityETH",
        "types": ["address", "uint256", "uint256", "uint256", "address", "uint256"],
        "param_names": [
            "token",
            "amountTokenDesired",
            "amountTokenMin",
            "amountETHMin",
            "to",
            "deadline",
        ],
    },
    "baa2abde": {
        "name": "removeLiquidity",
        "types": [
            "address",
            "address",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "uint256",
        ],
        "param_names": [
            "tokenA",
            "tokenB",
            "liquidity",
            "amountAMin",
            "amountBMin",
            "to",
            "deadline",
        ],
    },
    "02751cec": {
        "name": "removeLiquidityETH",
        "types": ["address", "uint256", "uint256", "uint256", "address", "uint256"],
        "param_names": [
            "token",
            "liquidity",
            "amountTokenMin",
            "amountETHMin",
            "to",
            "deadline",
        ],
    },
    # ── Uniswap V3 Router (struct params — name only, no param decoding) ─
    "ac9650d8": {
        "name": "multicall",
        "types": ["bytes[]"],
        "param_names": ["data"],
    },
    "5ae401dc": {
        "name": "multicall",
        "types": ["uint256", "bytes[]"],
        "param_names": ["deadline", "data"],
    },
    # exactInput(bytes,address,uint256,uint256,uint256) — decoded in :func:`_decode_exact_input`
    "414bf389": {"name": "exactInputSingle", "types": None, "param_names": None},
    "f28c0498": {"name": "exactOutput", "types": None, "param_names": None},
    "db3e2198": {"name": "exactOutputSingle", "types": None, "param_names": None},
}


# ---------------------------------------------------------------------------
# Known event topic0 hashes (pre-computed keccak256)
# ---------------------------------------------------------------------------

_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
_SWAP_V2_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
_SWAP_V3_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
_SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"


# Revert selectors (Error(string) = first 4 bytes of keccak256("Error(string)"))
_ERROR_SELECTOR = bytes.fromhex("08c379a0")
_PANIC_SELECTOR = bytes.fromhex("4e487b71")  # Panic(uint256)

_PANIC_CODES: dict[int, str] = {
    0x00: "generic compiler panic",
    0x01: "assertion failed",
    0x11: "arithmetic overflow/underflow",
    0x12: "division by zero",
    0x21: "invalid enum value",
    0x22: "invalid storage access",
    0x31: "pop on empty array",
    0x32: "array index out of bounds",
    0x41: "out of memory",
    0x51: "zero-initialized function pointer",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_hex_topic(raw: object) -> str:
    """Normalize a topic (bytes or HexBytes) to a ``0x…`` lowercase string."""
    if isinstance(raw, (bytes, bytearray)):
        return "0x" + raw.hex()
    return str(raw).lower()


def _format_decoded_value(value: Any, type_str: str) -> Any:
    """Format ABI-decoded values into human-friendly forms."""
    if "address" in type_str and not type_str.endswith("[]"):
        return to_checksum_address(value)
    if type_str == "address[]":
        return [to_checksum_address(a) for a in value]
    return value


def _signature_from_spec(spec: dict[str, Any]) -> str | None:
    """ABI-style ``name(type1,type2,...)`` for display."""
    name = spec.get("name")
    if not name:
        return None
    types = spec.get("types")
    if types is None:
        return name
    if not types:
        return f"{name}()"
    return f"{name}({','.join(types)})"


def _function_decode_result(
    *,
    function: str,
    selector_hex: str,
    signature: str | None,
    params: dict[str, Any] | None,
    param_names: list[str] | None,
    raw_data: str,
) -> dict[str, Any]:
    """Single shape for ``decode_function_call`` return values (DRY)."""
    return {
        "function": function,
        "selector": selector_hex,
        "signature": signature,
        "params": params,
        "param_names": param_names,
        "raw_data": raw_data,
    }


def _calldata_to_bytes(data: bytes | str) -> bytes:
    if isinstance(data, str):
        raw_hex = data.replace("0x", "")
        return bytes.fromhex(raw_hex)
    return data


def _log_data_bytes(log: dict[str, Any]) -> bytes:
    """Extract non-indexed log payload as bytes."""
    data_hex = log.get("data", "0x")
    if isinstance(data_hex, (bytes, bytearray)):
        data_hex = "0x" + data_hex.hex()
    if data_hex in ("0x", "", None):
        return b""
    return bytes.fromhex(str(data_hex).replace("0x", ""))


def _address_from_topic(topics: list, idx: int) -> Optional[str]:
    """Decode a 20-byte address from an indexed topic (right-padded to 32 bytes)."""
    if idx >= len(topics):
        return None
    raw = topics[idx]
    if isinstance(raw, (bytes, bytearray)):
        return to_checksum_address(raw[-20:])
    hex_str = str(raw).replace("0x", "")
    return to_checksum_address(bytes.fromhex(hex_str)[-20:])


def _decode_exact_input(params_data: bytes, raw_data: str) -> dict[str, Any]:
    """Decode Uniswap V3 ``exactInput(ExactInputParams)`` calldata."""
    try:
        (inner,) = abi_decode(["(bytes,address,uint256,uint256,uint256)"], params_data)
        path_b, recipient, deadline, amount_in, amount_out_min = inner
        params = {
            "path": path_b,
            "recipient": to_checksum_address(recipient),
            "deadline": deadline,
            "amountIn": amount_in,
            "amountOutMinimum": amount_out_min,
        }
        param_names = ["path", "recipient", "deadline", "amountIn", "amountOutMinimum"]
        return _function_decode_result(
            function="exactInput",
            selector_hex="c04b8d59",
            signature="exactInput((bytes,address,uint256,uint256,uint256))",
            params=params,
            param_names=param_names,
            raw_data=raw_data,
        )
    except Exception as err:
        logger.warning("exactInput ABI decode failed: %s", err)
        return _function_decode_result(
            function="exactInput",
            selector_hex="c04b8d59",
            signature="exactInput",
            params=None,
            param_names=None,
            raw_data=raw_data,
        )


def _try_decode_uint256(data_bytes: bytes) -> Optional[int]:
    if not data_bytes:
        return None
    try:
        (value,) = abi_decode(["uint256"], data_bytes)
        return value
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TransactionDecoder:
    """Stateless decoder for transaction calldata, event logs, and revert data."""

    @staticmethod
    def decode_function_call(data: bytes | str) -> dict[str, Any]:
        """Decode calldata into function name, ABI signature, and parameters.

        Args:
            data: Raw calldata as ``bytes`` or hex ``str`` (with or without ``0x``).

        Returns:
            Dict with keys: ``function``, ``selector``, ``signature``, ``params``,
            ``param_names``, ``raw_data`` (see module docstring for semantics).
        """
        validate_calldata_input(data)
        data = _calldata_to_bytes(data)
        raw_data = "0x" + data.hex()

        if len(data) < 4:
            logger.debug("calldata too short for selector: len=%s", len(data))
            return _function_decode_result(
                function="unknown",
                selector_hex=data.hex() if data else "",
                signature=None,
                params=None,
                param_names=None,
                raw_data=raw_data,
            )

        selector_hex = data[:4].hex()
        params_data = data[4:]

        if selector_hex == "c04b8d59":
            return _decode_exact_input(params_data, raw_data)

        spec = _FUNCTION_SELECTORS.get(selector_hex)

        if spec is None:
            logger.debug("unknown selector: %s", selector_hex)
            return _function_decode_result(
                function="unknown",
                selector_hex=selector_hex,
                signature=None,
                params=None,
                param_names=None,
                raw_data=raw_data,
            )

        if spec["types"] is None:
            return _function_decode_result(
                function=spec["name"],
                selector_hex=selector_hex,
                signature=spec["name"],
                params=None,
                param_names=None,
                raw_data=raw_data,
            )

        if not spec["types"]:
            return _function_decode_result(
                function=spec["name"],
                selector_hex=selector_hex,
                signature=_signature_from_spec(spec),
                params={},
                param_names=[],
                raw_data=raw_data,
            )

        try:
            decoded_values = abi_decode(spec["types"], params_data)
            params = {
                name: _format_decoded_value(val, t)
                for name, val, t in zip(
                    spec["param_names"], decoded_values, spec["types"], strict=True
                )
            }
        except Exception as err:
            logger.warning("ABI decode failed for selector %s: %s", selector_hex, err)
            params = None

        return _function_decode_result(
            function=spec["name"],
            selector_hex=selector_hex,
            signature=_signature_from_spec(spec),
            params=params,
            param_names=list(spec["param_names"]) if params is not None else None,
            raw_data=raw_data,
        )

    @staticmethod
    def parse_event(log: Mapping[str, Any]) -> dict[str, Any]:
        """Parse one log entry into event name + decoded fields when recognized.

        Args:
            log: Web3-style log dict (``topics``, ``data``, ``address``).

        Returns:
            Dict with ``name``, ``address``, ``decoded``, and ``raw`` (original log).
        """
        validate_log_dict(log)
        topics = log.get("topics", [])
        if not topics:
            return {
                "name": "UnknownEvent",
                "address": str(log.get("address", "")),
                "decoded": None,
                "raw": log,
            }

        topic0 = _to_hex_topic(topics[0])
        address = str(log.get("address", ""))
        data_bytes = _log_data_bytes(log)

        # Transfer / Approval share uint256 in data
        if topic0 == _TRANSFER_TOPIC:
            value = _try_decode_uint256(data_bytes)
            return {
                "name": "Transfer",
                "address": address,
                "decoded": {
                    "from": _address_from_topic(topics, 1),
                    "to": _address_from_topic(topics, 2),
                    "value": value,
                },
                "raw": log,
            }

        if topic0 == _APPROVAL_TOPIC:
            value = _try_decode_uint256(data_bytes)
            return {
                "name": "Approval",
                "address": address,
                "decoded": {
                    "owner": _address_from_topic(topics, 1),
                    "spender": _address_from_topic(topics, 2),
                    "value": value,
                },
                "raw": log,
            }

        if topic0 == _SWAP_V2_TOPIC:
            try:
                vals = abi_decode(
                    ["uint256", "uint256", "uint256", "uint256"],
                    data_bytes,
                )
            except Exception:
                vals = (None, None, None, None)
            return {
                "name": "Swap",
                "address": address,
                "decoded": {
                    "sender": _address_from_topic(topics, 1),
                    "amount0In": vals[0],
                    "amount1In": vals[1],
                    "amount0Out": vals[2],
                    "amount1Out": vals[3],
                    "to": _address_from_topic(topics, 2),
                },
                "raw": log,
            }

        if topic0 == _SWAP_V3_TOPIC:
            try:
                vals = abi_decode(
                    ["int256", "int256", "uint160", "uint128", "int24"],
                    data_bytes,
                )
            except Exception:
                vals = (None, None, None, None, None)
            return {
                "name": "SwapV3",
                "address": address,
                "decoded": {
                    "sender": _address_from_topic(topics, 1),
                    "recipient": _address_from_topic(topics, 2),
                    "amount0": vals[0],
                    "amount1": vals[1],
                    "sqrtPriceX96": vals[2],
                    "liquidity": vals[3],
                    "tick": vals[4],
                },
                "raw": log,
            }

        if topic0 == _SYNC_TOPIC:
            try:
                vals = abi_decode(["uint112", "uint112"], data_bytes)
            except Exception:
                vals = (None, None)
            return {
                "name": "Sync",
                "address": address,
                "decoded": {"reserve0": vals[0], "reserve1": vals[1]},
                "raw": log,
            }

        return {"name": "UnknownEvent", "address": address, "decoded": None, "raw": log}

    @staticmethod
    def parse_events(logs: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Parse a list of event logs (same rules as :meth:`parse_event`)."""
        validate_logs_list(logs)
        return [TransactionDecoder.parse_event(log) for log in logs]

    @staticmethod
    def humanize_revert_tuple_string(text: str) -> str:
        """If *text* is a repr like ``('execution reverted: …', '0x08c379a0…')``, decode bytes.

        Web3's :class:`web3.exceptions.ContractLogicError` ``str()`` often looks like that;
        this returns a single readable line (message, or ABI-decoded string, deduplicated).
        """
        cleaned = text.strip()
        if not (cleaned.startswith("(") and cleaned.endswith(")")):
            return text
        try:
            parsed = ast.literal_eval(cleaned)
        except (ValueError, SyntaxError, MemoryError):
            return text
        if not isinstance(parsed, tuple) or len(parsed) < 2:
            return text
        msg, payload = parsed[0], parsed[1]
        if not isinstance(msg, str) or not isinstance(payload, str):
            return text
        if not payload.startswith("0x") or len(payload) < 10:
            return msg.strip()

        decoded = TransactionDecoder.decode_revert_reason(payload)
        if not decoded:
            return msg.strip()
        if decoded in msg:
            return msg.strip()
        return decoded

    @staticmethod
    def decode_revert_reason(data: bytes | str) -> Optional[str]:
        """Extract a human-readable revert reason from error or return data bytes.

        Args:
            data: Raw revert payload (``Error(string)`` or ``Panic(uint256)``).

        Returns:
            Decoded string, or ``None`` if not recognized.
        """
        if data is None:
            raise InvalidParameterError("revert data must not be None.")
        if isinstance(data, str):
            raw_hex = data.replace("0x", "").strip()
            if not raw_hex:
                return None
            try:
                data = bytes.fromhex(raw_hex)
            except ValueError as err:
                raise InvalidParameterError("revert data hex is invalid.") from err
        elif not isinstance(data, (bytes, bytearray, memoryview)):
            raise InvalidParameterError("revert data must be str or bytes-like.")
        else:
            data = bytes(data)

        if len(data) < 4:
            return None

        selector = data[:4]

        if selector == _ERROR_SELECTOR:
            try:
                (reason,) = abi_decode(["string"], data[4:])
                return reason
            except Exception:
                return None

        if selector == _PANIC_SELECTOR:
            try:
                (code,) = abi_decode(["uint256"], data[4:])
                desc = _PANIC_CODES.get(code, "unknown")
                return f"Panic({hex(code)}): {desc}"
            except Exception:
                return None

        return None
