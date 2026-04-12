"""Tests for Uniswap V3 QuoterV2 decode (no live RPC)."""

from eth_abi import encode as abi_encode

from pricing.liquidity_pool import QuoteResult
from pricing.uniswap_v3_quoter import decode_quote_exact_input_single_return


def test_decode_quote_exact_input_single_return() -> None:
    amount_out = 12345
    sqrt_after = 2**96
    ticks = 3
    gas_est = 200_000
    data = abi_encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [amount_out, sqrt_after, ticks, gas_est],
    )
    r = decode_quote_exact_input_single_return(data)
    assert r == QuoteResult(amount_out=amount_out, gas_estimate=gas_est)
