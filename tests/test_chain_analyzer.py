"""Unit tests for :mod:`chain.analyzer` helpers (no RPC)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from chain.analyzer import (
    _fmt_bytes_compact,
    _fmt_number,
    _format_abi_value_preview,
    _format_arg_value,
    _short_addr,
    _wei_to_eth,
    _wei_to_gwei,
    decode_uniswap_v3_path,
)
from chain.errors import InvalidParameterError
from chain.validation import normalize_tx_hash


def test_decode_uniswap_v3_path_two_hops():
    from eth_utils import to_checksum_address

    path_bytes = (
        bytes.fromhex("a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        + (3000).to_bytes(3, "big")
        + bytes.fromhex("c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")
    )
    out = decode_uniswap_v3_path(path_bytes)
    assert len(out) == 2
    assert out[0] == to_checksum_address("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
    assert out[1] == to_checksum_address("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")


def test_normalize_tx_hash_accepts_uppercase_and_lowercases():
    h = "a" * 64  # valid hex, 64 chars
    out = normalize_tx_hash("0x" + h.upper())
    assert out == "0x" + h


def test_normalize_tx_hash_adds_0x_prefix():
    h = "b" * 64
    out = normalize_tx_hash(h)
    assert out == "0x" + h


def test_normalize_tx_hash_rejects_40_char_address():
    with pytest.raises(InvalidParameterError, match="40 hex"):
        normalize_tx_hash("0x" + "c" * 40)


def test_normalize_tx_hash_rejects_bad_length():
    with pytest.raises(InvalidParameterError, match="64 hex"):
        normalize_tx_hash("0x" + "d" * 32)


def test_normalize_tx_hash_rejects_non_hex():
    with pytest.raises(InvalidParameterError, match="hexadecimal"):
        normalize_tx_hash("0x" + "g" * 64)


def test_normalize_tx_hash_rejects_empty_after_prefix():
    with pytest.raises(InvalidParameterError, match="Empty"):
        normalize_tx_hash("0x")


def test_normalize_tx_hash_strips_whitespace():
    h = "a" * 64
    assert normalize_tx_hash(f"  0x{h}  ") == f"0x{h}"


# ── Formatting helpers ──────────────────────────────────────────────


class TestFmtNumber:
    def test_thousands_separator(self):
        assert _fmt_number(1_234_567) == "1,234,567"

    def test_zero(self):
        assert _fmt_number(0) == "0"

    def test_small(self):
        assert _fmt_number(42) == "42"


class TestWeiToGwei:
    def test_exact_gwei(self):
        assert _wei_to_gwei(1_000_000_000) == Decimal("1")

    def test_fractional(self):
        assert _wei_to_gwei(1_500_000_000) == Decimal("1.5")

    def test_zero(self):
        assert _wei_to_gwei(0) == Decimal("0")


class TestWeiToEth:
    def test_one_eth(self):
        assert _wei_to_eth(10**18) == Decimal("1")

    def test_fractional_eth(self):
        assert _wei_to_eth(5 * 10**17) == Decimal("0.5")

    def test_zero(self):
        assert _wei_to_eth(0) == Decimal("0")


def test_fmt_bytes_compact_truncates_long_payload():
    b = bytes(range(100))
    out = _fmt_bytes_compact(b)
    assert out.startswith("0x")
    assert "…" in out
    assert "100 bytes" in out
    assert "00010203040506070809" in out  # start of range() bytes hex


def test_format_abi_value_preview_tuple_of_bytes():
    a, c = b"hello", b"x" * 80
    out = _format_abi_value_preview((a, c))
    assert "hello" in out or "68656c6c6f" in out
    assert "80 bytes" in out
    assert "\\x" not in out  # not Python bytes repr


def test_format_arg_value_data_tuple_bytes():
    client = MagicMock()
    tx: dict = {}
    params: dict = {}
    raw = (b"abc", b"d" * 100)
    out = _format_arg_value("data", raw, "multicall", params, tx, client)
    assert "\\x" not in out
    assert "bytes" in out or "0x" in out


def test_format_arg_value_swap_respects_symbol_decimals_order():
    """token_symbol_and_decimals returns (symbol, decimals); formatting must not swap them."""
    client = MagicMock()
    client.token_cache.get.return_value = {"symbol": "DAI", "decimals": 18}
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    dai = "0x6b175474e89094c44da98b954eedeac495271d0f"
    tx = {"to": weth}
    params = {"path": [weth, dai]}
    raw = 10**18
    out = _format_arg_value(
        "amountOutMin",
        raw,
        "swapExactETHForTokens",
        params,
        tx,
        client,
    )
    assert "DAI" in out
    assert "1.0000" in out


class TestShortAddr:
    def test_long_address_truncated(self):
        addr = "0x" + "a" * 40
        result = _short_addr(addr)
        assert result.startswith("0x")
        assert "..." in result

    def test_none_returns_question_mark(self):
        assert _short_addr(None) == "?"

    def test_short_string_unchanged(self):
        assert _short_addr("0xabcd") == "0xabcd"
