"""Tests for :mod:`pricing.raw_amount_list`."""

import pytest

from pricing.raw_amount_list import parse_raw_amount_ints


def test_comma_separated() -> None:
    assert parse_raw_amount_ints("1e18,5e17") == [10**18, 5 * 10**17]


def test_whitespace_separated_powershell_style() -> None:
    """Shell may pass one argument with spaces instead of commas."""
    assert parse_raw_amount_ints("1E+17 5E+17 1E+18 5E+18") == [
        10**17,
        5 * 10**17,
        10**18,
        5 * 10**18,
    ]


def test_mixed_comma_and_space() -> None:
    assert parse_raw_amount_ints("1000000, 10000000 50000000") == [10**6, 10**7, 50 * 10**6]


def test_underscores() -> None:
    assert parse_raw_amount_ints("1_000_000") == [10**6]


def test_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_raw_amount_ints("0")
    with pytest.raises(ValueError, match="positive"):
        parse_raw_amount_ints("1 -1")


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid raw amount"):
        parse_raw_amount_ints("1e18 not_a_number")
