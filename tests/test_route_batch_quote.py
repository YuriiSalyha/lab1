"""Tests for :mod:`pricing.route_batch_quote`."""

from unittest.mock import MagicMock

import pytest
from web3 import Web3

from core.types import Address, Token
from pricing.batch_quote import BatchQuoteExecutor
from pricing.liquidity_pool import QuoteResult, UniswapV2PoolAdapter
from pricing.route import Route
from pricing.route_batch_quote import batch_quote_route_outputs
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter
from pricing.uniswap_v3_quoter import QUOTER_V2_MAINNET

A1 = Address("0x1111111111111111111111111111111111111111")
A2 = Address("0x2222222222222222222222222222222222222222")
PAIR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
A3 = Address("0x3333333333333333333333333333333333333333")
P_B = Address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")


def _tok(addr: Address, sym: str) -> Token:
    return Token(address=addr, symbol=sym, decimals=18)


def test_batch_v2_only_never_calls_executor() -> None:
    t1, t2, t3 = _tok(A1, "A"), _tok(A2, "B"), _tok(A3, "C")
    p0 = UniswapV2Pair(PAIR, t1, t2, 10**24, 10**24, 30)
    p1 = UniswapV2Pair(
        P_B,
        t2,
        t3,
        10**24,
        10**24,
        30,
    )
    route = Route(
        [UniswapV2PoolAdapter(p0), UniswapV2PoolAdapter(p1)],
        [t1, t2, t3],
    )
    ex = MagicMock(spec=BatchQuoteExecutor)
    out = batch_quote_route_outputs(route, [10**18], ex, chunk_size=50)
    assert len(out) == 1
    amt, final, _gas = out[0]
    assert amt == 10**18
    assert final is not None
    ex.execute_quote_results.assert_not_called()


def test_batch_v3_single_hop_uses_executor() -> None:
    w3 = Web3()
    t0 = _tok(
        Address("0x0000000000000000000000000000000000000001"),
        "T0",
    )
    t1 = _tok(
        Address("0x0000000000000000000000000000000000000002"),
        "T1",
    )
    client = MagicMock()
    client.w3 = w3
    pool_addr = Address("0x00000000000000000000000000000000000000aa")
    v3 = UniswapV3PoolQuoter(
        pool_addr,
        client,
        token0=t0,
        token1=t1,
        fee=3000,
        quoter_address=QUOTER_V2_MAINNET,
    )
    route = Route([v3], [t0, t1])
    ex = MagicMock(spec=BatchQuoteExecutor)
    ex.execute_quote_results.return_value = [QuoteResult(amount_out=12345, gas_estimate=99999)]

    out = batch_quote_route_outputs(route, [10**12], ex, chunk_size=50)
    assert out == [(10**12, 12345, 99999)]
    ex.execute_quote_results.assert_called_once()
    call_kw = ex.execute_quote_results.call_args.kwargs
    assert "decode" in call_kw
    reqs = ex.execute_quote_results.call_args.args[0]
    assert len(reqs) == 1
    assert reqs[0].target.startswith("0x")


def test_batch_v3_failure_marks_amount_invalid() -> None:
    w3 = Web3()
    t0 = _tok(Address("0x0000000000000000000000000000000000000001"), "T0")
    t1 = _tok(Address("0x0000000000000000000000000000000000000002"), "T1")
    client = MagicMock()
    client.w3 = w3
    v3 = UniswapV3PoolQuoter(
        Address("0x00000000000000000000000000000000000000aa"),
        client,
        token0=t0,
        token1=t1,
        fee=3000,
        quoter_address=QUOTER_V2_MAINNET,
    )
    route = Route([v3], [t0, t1])
    ex = MagicMock(spec=BatchQuoteExecutor)
    ex.execute_quote_results.return_value = [ValueError("revert")]
    out = batch_quote_route_outputs(route, [10**12], ex)
    assert out == [(10**12, None, 0)]


@pytest.mark.parametrize("chunk_size", [1, 5])
def test_batch_v3_chunks_split_calls(chunk_size: int) -> None:
    w3 = Web3()
    t0 = _tok(Address("0x0000000000000000000000000000000000000001"), "T0")
    t1 = _tok(Address("0x0000000000000000000000000000000000000002"), "T1")
    client = MagicMock()
    client.w3 = w3
    v3 = UniswapV3PoolQuoter(
        Address("0x00000000000000000000000000000000000000aa"),
        client,
        token0=t0,
        token1=t1,
        fee=3000,
        quoter_address=QUOTER_V2_MAINNET,
    )
    route = Route([v3], [t0, t1])
    ex = MagicMock(spec=BatchQuoteExecutor)
    _next_out = {"n": 0}

    def _fake(reqs: list, **kwargs: object) -> list[QuoteResult]:
        out: list[QuoteResult] = []
        for _ in reqs:
            i = _next_out["n"]
            _next_out["n"] += 1
            out.append(QuoteResult(amount_out=100 + i, gas_estimate=10))
        return out

    ex.execute_quote_results.side_effect = _fake
    amounts = [10, 20, 30, 40, 50]
    out = batch_quote_route_outputs(route, amounts, ex, chunk_size=chunk_size)
    assert len(out) == 5
    assert ex.execute_quote_results.call_count == (5 + chunk_size - 1) // chunk_size
    for i, (a, f, g) in enumerate(out):
        assert a == amounts[i]
        assert f == 100 + i
        assert g == 10
