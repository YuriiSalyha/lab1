import json
from typing import Any

from eth_utils import keccak

from core.errors import WalletValidationError


class CanonicalSerializer:
    """
    Produces deterministic JSON for signing.

    Rules / guarantees:
    - Keys sorted alphabetically at every nesting level
    - No whitespace in the JSON output
    - Unicode preserved (emoji and non-ASCII safe)
    - Very large integers serialized as exact decimal values
    - Floats rejected outright (NaN / Infinity / rounding hazards)
    - Dict keys must be strings (JSON spec requirement)
    - Sets rejected (non-deterministic iteration order)
    """

    @staticmethod
    def _validate(obj: Any, *, _path: str = "$") -> None:
        """Recursively validate the entire object graph before serialization."""
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
        """Returns canonical UTF-8 bytes of the JSON representation."""
        CanonicalSerializer._validate(obj)

        try:
            serialized = json.dumps(
                obj,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            return serialized.encode("utf-8")
        except (TypeError, ValueError) as e:
            raise WalletValidationError(f"Failed to serialize object: {e}") from None

    @staticmethod
    def deserialize(data: bytes) -> Any:
        """Parse canonical JSON bytes back into a Python object."""
        if not isinstance(data, bytes):
            raise WalletValidationError("Expected bytes input")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise WalletValidationError(f"Failed to deserialize: {e}") from None

    @staticmethod
    def hash(obj: Any) -> bytes:
        """Returns keccak256 of canonical serialization."""
        return keccak(CanonicalSerializer.serialize(obj))

    @staticmethod
    def verify_determinism(obj: Any, iterations: int = 100) -> bool:
        """
        Verifies serialization is deterministic over *iterations* runs.

        Useful as a smoke-test: same object reference must always produce
        identical bytes.  For cross-object determinism (different insertion
        orders), build separate dicts in the test and compare directly.
        """
        if iterations <= 0:
            raise WalletValidationError("iterations must be a positive integer")

        first = CanonicalSerializer.serialize(obj)
        return all(CanonicalSerializer.serialize(obj) == first for _ in range(iterations - 1))
