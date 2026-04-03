"""Input validation helpers for the ``chain`` package (RPC URLs, tx hashes, addresses)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eth_utils import is_address

from chain.errors import InvalidParameterError

_VALID_GAS_PRIORITIES = frozenset({"low", "medium", "high"})


def normalize_tx_hash(value: str) -> str:
    """Normalize *value* to 0x + 64 lowercase hex digits or raise `InvalidParameterError`"""
    if not isinstance(value, str):
        raise InvalidParameterError("Transaction hash must be a string.")
    s = value.strip()
    if not s.startswith("0x"):
        s = "0x" + s
    hex_part = s[2:]
    if not hex_part:
        raise InvalidParameterError("Empty transaction hash.")
    try:
        int(hex_part, 16)
    except ValueError as err:
        raise InvalidParameterError(
            "Transaction hash must contain only hexadecimal digits."
        ) from err
    if len(hex_part) != 64:
        if len(hex_part) == 40:
            raise InvalidParameterError(
                "This value has 40 hex digits (20 bytes), like an address, not a "
                "transaction hash. A tx hash must be 64 hex digits (32 bytes)."
            )
        raise InvalidParameterError(
            f"Transaction hash must be exactly 64 hex digits (32 bytes); got {len(hex_part)}."
        )
    return "0x" + hex_part.lower()


def validate_rpc_urls(urls: list[str]) -> None:
    """Ensure *urls* is a non-empty list of non-empty HTTP(S) URL strings."""
    if not isinstance(urls, list):
        raise InvalidParameterError("rpc_urls must be a list of URL strings.")
    if not urls:
        raise InvalidParameterError("At least one RPC URL is required.")
    for i, url in enumerate(urls):
        if not isinstance(url, str) or not url.strip():
            raise InvalidParameterError(f"rpc_urls[{i}] must be a non-empty string.")
        u = url.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            raise InvalidParameterError(
                f"rpc_urls[{i}] must start with http:// or https:// (got {u!r})."
            )


def validate_timeout_seconds(name: str, value: int, *, minimum: int = 1) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParameterError(f"{name} must be an integer.")
    if value < minimum:
        raise InvalidParameterError(f"{name} must be >= {minimum}, got {value}.")


def validate_max_retries(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParameterError("max_retries must be an integer.")
    if value < 1:
        raise InvalidParameterError(f"max_retries must be >= 1, got {value}.")


def validate_gas_priority(priority: str) -> None:
    if priority not in _VALID_GAS_PRIORITIES:
        raise InvalidParameterError(
            f"priority must be one of {sorted(_VALID_GAS_PRIORITIES)}, got {priority!r}."
        )


def validate_buffer_bps(buffer_bps: int) -> None:
    if not isinstance(buffer_bps, int) or isinstance(buffer_bps, bool):
        raise InvalidParameterError("buffer_bps must be an integer.")
    if buffer_bps < 0:
        raise InvalidParameterError(f"buffer_bps must be non-negative, got {buffer_bps}.")


def validate_token_address_str(token_address: str, *, param_name: str = "token_address") -> None:
    if not isinstance(token_address, str) or not token_address.strip():
        raise InvalidParameterError(f"{param_name} must be a non-empty string.")
    addr = token_address.strip()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    if not is_address(addr):
        raise InvalidParameterError(f"{param_name} is not a valid Ethereum address: \
         {token_address!r}.")


def validate_eth_address_str(address: str, *, param_name: str = "address") -> None:
    validate_token_address_str(address, param_name=param_name)


def require_bytes(name: str, value: Any, *, allow_empty: bool = True) -> None:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise InvalidParameterError(f"{name} must be bytes-like.")
    if not allow_empty and len(value) == 0:
        raise InvalidParameterError(f"{name} must be non-empty.")


def validate_calldata_input(data: Any) -> None:
    if data is None:
        raise InvalidParameterError("calldata must not be None.")
    if isinstance(data, str):
        return
    if isinstance(data, (bytes, bytearray, memoryview)):
        return
    raise InvalidParameterError("calldata must be str or bytes-like.")


def validate_block_identifier(block: str | int) -> None:
    if isinstance(block, bool):
        raise InvalidParameterError("block must be a block tag string or an integer block number.")
    if isinstance(block, int):
        if block < 0:
            raise InvalidParameterError(f"block number must be non-negative, got {block}.")
        return
    if not isinstance(block, str) or not block.strip():
        raise InvalidParameterError("block tag must be a non-empty string (e.g. 'latest').")
    b = block.strip().lower()
    allowed = frozenset({"latest", "earliest", "pending", "safe", "finalized"})
    if b in allowed:
        return
    if b.startswith("0x"):
        try:
            int(b[2:], 16)
        except ValueError as err:
            raise InvalidParameterError(f"Invalid block hash hex: {block!r}.") from err
        return
    raise InvalidParameterError(
        f"Invalid block identifier {block!r}; use an int, 'latest', or a 0x-prefixed block hash."
    )


def validate_tx_dict(tx: Any) -> None:
    if not isinstance(tx, dict):
        raise InvalidParameterError("tx must be a dictionary.")


def validate_log_dict(log: Any) -> None:
    # Web3.py v7+ returns ReadableAttributeDict for logs; it is a Mapping but not dict.
    if not isinstance(log, Mapping):
        raise InvalidParameterError("log must be a dict-like mapping (e.g. RPC log entry).")


def validate_logs_list(logs: Any) -> None:
    if not isinstance(logs, list):
        raise InvalidParameterError("logs must be a list.")
