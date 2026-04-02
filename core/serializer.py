"""Deterministic JSON encoding for signing and hashing (sorted keys, no floats)."""

from __future__ import annotations

import json
import logging
from typing import Any

from eth_utils import keccak

from core.errors import WalletValidationError

logger = logging.getLogger(__name__)


class CanonicalSerializer:
    """Deterministic JSON serialization for cryptographic hashing.

    Guarantees:
    - Keys sorted at every nesting level
    - Compact separators (no spaces)
    - Unicode preserved
    - Large integers as exact JSON numbers
    - Rejects floats and sets (non-deterministic or unsafe)
    """

    @staticmethod
    def _validate(obj: Any, *, _path: str = "$") -> None:
        """Reject unsupported types before ``json.dumps``; *path* is for error messages."""
        if isinstance(obj, float):
            raise WalletValidationError(
                f"Floating point value at {_path}. " "Use strings or integers instead."
            )

        if isinstance(obj, set):
            raise WalletValidationError(
                f"Set at {_path} is not allowed (non-deterministic order). "
                "Convert to a sorted list first."
            )

        if isinstance(obj, dict):
            for key, value in obj.items():
                if not isinstance(key, str):
                    raise WalletValidationError(
                        f"Non-string dict key {key!r} at {_path}. " "JSON requires string keys."
                    )
                CanonicalSerializer._validate(value, _path=f"{_path}.{key}")
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                CanonicalSerializer._validate(item, _path=f"{_path}[{i}]")

    @staticmethod
    def serialize(obj: Any) -> bytes:
        """
        Args:
            obj: JSON-serializable Python structure (see class rules).

        Returns:
            UTF-8 bytes of canonical JSON.

        Raises:
            WalletValidationError: Invalid type or ``json.dumps`` failure.
        """
        CanonicalSerializer._validate(obj)

        try:
            serialized = json.dumps(
                obj,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as e:
            logger.warning("serialize failed: %s", e)
            raise WalletValidationError(f"Failed to serialize object: {e}") from None

        out = serialized.encode("utf-8")
        logger.debug("serialized %s bytes", len(out))
        return out

    @staticmethod
    def deserialize(data: bytes) -> Any:
        """
        Args:
            data: UTF-8 JSON bytes.

        Returns:
            Parsed Python object.

        Raises:
            WalletValidationError: Not bytes or invalid JSON.
        """
        if not isinstance(data, bytes):
            raise WalletValidationError("Expected bytes input")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("deserialize failed: %s", e)
            raise WalletValidationError(f"Failed to deserialize: {e}") from None

    @staticmethod
    def hash(obj: Any) -> bytes:
        """
        Args:
            obj: Same constraints as :meth:`serialize`.

        Returns:
            Keccak-256 digest of canonical UTF-8 JSON bytes.
        """
        digest = keccak(CanonicalSerializer.serialize(obj))
        logger.debug("hash produced %s-byte digest", len(digest))
        return digest

    @staticmethod
    def verify_determinism(obj: Any, iterations: int = 100) -> bool:
        """
        Check that :meth:`serialize` is stable across repeated calls (same object).

        Args:
            obj: Value to serialize.
            iterations: Number of runs (must be positive).

        Returns:
            True if every serialization matches the first.

        Raises:
            WalletValidationError: *iterations* <= 0.
        """
        if iterations <= 0:
            raise WalletValidationError("iterations must be a positive integer")

        first = CanonicalSerializer.serialize(obj)
        return all(CanonicalSerializer.serialize(obj) == first for _ in range(iterations - 1))
