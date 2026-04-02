"""Unit tests for :mod:`chain.analyzer` helpers (no RPC)."""

from __future__ import annotations

import pytest

from chain.analyzer import _normalize_tx_hash


def test_normalize_tx_hash_accepts_uppercase_and_lowercases():
    h = "a" * 64  # valid hex, 64 chars
    out = _normalize_tx_hash("0x" + h.upper())
    assert out == "0x" + h


def test_normalize_tx_hash_adds_0x_prefix():
    h = "b" * 64
    out = _normalize_tx_hash(h)
    assert out == "0x" + h


def test_normalize_tx_hash_rejects_40_char_address():
    with pytest.raises(ValueError, match="40 hex"):
        _normalize_tx_hash("0x" + "c" * 40)


def test_normalize_tx_hash_rejects_bad_length():
    with pytest.raises(ValueError, match="64 hex"):
        _normalize_tx_hash("0x" + "d" * 32)


def test_normalize_tx_hash_rejects_non_hex():
    with pytest.raises(ValueError, match="hexadecimal"):
        _normalize_tx_hash("0x" + "g" * 64)
