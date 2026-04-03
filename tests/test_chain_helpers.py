"""Unit tests for :mod:`chain.helpers`."""

from __future__ import annotations

from unittest.mock import MagicMock

from chain.helpers import format_human_token_amount, token_symbol_and_decimals


def test_format_human_token_amount():
    s = format_human_token_amount(1_500_000, 6, "USDC")
    assert "1.5000" in s
    assert "USDC" in s


def test_format_human_token_amount_none_raw():
    s = format_human_token_amount(None, 18, "ETH")
    assert s.startswith("?")


def test_token_symbol_and_decimals_success():
    client = MagicMock()
    client.token_cache.get.return_value = {"symbol": "WETH", "decimals": 18}

    sym, dec = token_symbol_and_decimals(client, "0x1111111111111111111111111111111111111111")
    assert sym == "WETH"
    assert dec == 18
    client.token_cache.get.assert_called_once()


def test_token_symbol_and_decimals_fallback_on_error():
    client = MagicMock()
    client.token_cache.get.side_effect = RuntimeError("RPC down")

    sym, dec = token_symbol_and_decimals(client, "0x2222222222222222222222222222222222222222")
    assert sym == "???"
    assert dec == 18
