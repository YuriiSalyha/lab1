"""Shared input validation for ``core`` (paths, non-empty strings, positive integers)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.errors import WalletValidationError


def require_non_empty_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WalletValidationError(f"{name} must be a non-empty string.")
    return value.strip()


def require_path_str(name: str, value: Any) -> str:
    return require_non_empty_str(name, value)


def validate_log_file_param(log_file: str | Path | None) -> None:
    if log_file is None:
        return
    if isinstance(log_file, Path):
        return
    if not isinstance(log_file, str) or not log_file.strip():
        raise WalletValidationError("log_file must be a non-empty string or Path.")


def validate_positive_int(name: str, value: Any, *, minimum: int = 1) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WalletValidationError(f"{name} must be an integer.")
    if value < minimum:
        raise WalletValidationError(f"{name} must be >= {minimum}, got {value}.")
