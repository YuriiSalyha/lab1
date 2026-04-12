"""Unit tests for :mod:`pricing.fork_simulator` (mocked RPC)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from eth_abi import encode
from web3.exceptions import ContractLogicError

from core.types import Address, Token
from pricing.fork_simulator import ForkSimulator, SimulationResult, _build_calldata_from_swap_params
from pricing.route import Route
from pricing.uniswap_v2_pair import UniswapV2Pair

ROUTER = Address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
SENDER = Address("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
RECV = Address("0x70997970C51812dc3A010C7d01b50e0d17dc79C8")

T0 = Address("0x1111111111111111111111111111111111111111")
T1 = Address("0x2222222222222222222222222222222222222222")
PAIR_ADDR = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def _tok(a: Address, sym: str, dec: int = 18) -> Token:
    return Token(address=a, symbol=sym, decimals=dec)


def _pair() -> UniswapV2Pair:
    w0 = _tok(T0, "A")
    w1 = _tok(T1, "B")
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=w0,
        token1=w1,
        reserve0=1_000_000 * 10**18,
        reserve1=2_000_000 * 10**18,
        fee_bps=30,
    )


def test_build_calldata_raw_vs_structured_equivalent() -> None:
    structured = {
        "function": "swapExactTokensForTokens",
        "amount_in": 100,
        "amount_out_min": 0,
        "path": [T0, T1],
        "to": SENDER,
        "deadline": 999,
    }
    data = _build_calldata_from_swap_params(structured)
    assert _build_calldata_from_swap_params({"data": data}) == data


def test_build_calldata_missing_data_raises() -> None:
    with pytest.raises(ValueError, match="function"):
        _build_calldata_from_swap_params({"path": [T0], "to": SENDER, "deadline": 1})


def test_simulate_swap_success_decodes_amounts() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    ret = encode(["uint256[]"], [[10, 20, 999]])
    mock_eth = MagicMock()
    mock_eth.call.return_value = ret
    mock_eth.estimate_gas.return_value = 180_000

    with patch.object(sim.w3, "eth", mock_eth):
        r = sim.simulate_swap(
            ROUTER,
            {
                "function": "swapExactTokensForTokens",
                "amount_in": 1,
                "amount_out_min": 0,
                "path": [T0, T1],
                "to": SENDER,
                "deadline": 1,
            },
            SENDER,
        )

    assert isinstance(r, SimulationResult)
    assert r.success is True
    assert r.amount_out == 999
    assert r.gas_used == 180_000
    assert r.error is None
    assert r.logs == []


def test_simulate_swap_contract_logic_error() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    err = ContractLogicError("execution reverted")
    revert = bytes.fromhex("08c379a0") + encode(["string"], ["insufficient liquidity"])
    err.data = "0x" + revert.hex()

    mock_eth = MagicMock()
    mock_eth.call.side_effect = err

    with patch.object(sim.w3, "eth", mock_eth):
        r = sim.simulate_swap(ROUTER, {"data": bytes.fromhex("38ed1739") + b"\x00" * 100}, SENDER)

    assert r.success is False
    assert r.amount_out == 0
    assert r.error is not None
    assert "insufficient liquidity" in (r.error or "")


def test_simulate_swap_invalid_params_error() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    r = sim.simulate_swap(ROUTER, {}, SENDER)
    assert r.success is False
    assert "data" in (r.error or "") or "function" in (r.error or "")


def test_simulate_route_delegates_and_gas_fallback() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    p = _pair()
    route = Route([p], [p.token0, p.token1])
    ret = encode(["uint256[]"], [[10**12, 42]])
    mock_eth = MagicMock()
    mock_eth.call.return_value = ret
    mock_eth.estimate_gas.side_effect = RuntimeError("no estimate")

    with patch.object(sim.w3, "eth", mock_eth):
        r = sim.simulate_route(
            ROUTER,
            route,
            amount_in=10**12,
            sender=SENDER,
            deadline=10**9,
            recipient=RECV,
            amount_out_min=1,
        )

    assert r.success
    assert r.amount_out == 42
    assert r.gas_used == route.estimate_gas(10**12)
    call_kw = mock_eth.call.call_args[0][0]
    assert call_kw["from"] == SENDER.checksum
    assert call_kw["to"] == ROUTER.checksum


def test_compare_simulation_vs_calculation_dict() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    p = _pair()
    amt_in = 10**18
    calculated = p.get_amount_out(amt_in, p.token0)
    ret = encode(["uint256[]"], [[amt_in, calculated]])
    mock_eth = MagicMock()
    mock_eth.call.return_value = ret
    mock_eth.estimate_gas.return_value = 150_000

    with patch.object(sim.w3, "eth", mock_eth):
        out = sim.compare_simulation_vs_calculation(
            ROUTER,
            p,
            amt_in,
            p.token0,
            SENDER,
        )

    assert out["calculated"] == calculated
    assert out["simulated"] == calculated
    assert out["difference"] == 0
    assert out["match"] is True
    assert out["error"] is None


def test_compare_invalid_token_raises() -> None:
    sim = ForkSimulator("http://127.0.0.1:9")
    p = _pair()
    other = _tok(Address("0x3333333333333333333333333333333333333333"), "X")
    with pytest.raises(ValueError, match="not a token"):
        sim.compare_simulation_vs_calculation(ROUTER, p, 10**9, other, SENDER)


def test_simulate_swap_accepts_hex_string_data() -> None:
    from chain.uniswap_v2_router import encode_uniswap_v2_swap_calldata

    data = encode_uniswap_v2_swap_calldata(
        "swapExactTokensForTokens",
        path=[T0, T1],
        to=SENDER,
        deadline=1,
        amount_in=5,
        amount_out_min=0,
    )
    sim = ForkSimulator("http://127.0.0.1:9")
    ret = encode(["uint256[]"], [[5, 7]])
    mock_eth = MagicMock()
    mock_eth.call.return_value = ret
    mock_eth.estimate_gas.return_value = 1

    with patch.object(sim.w3, "eth", mock_eth):
        r = sim.simulate_swap(ROUTER, {"data": "0x" + data.hex()}, SENDER)
    assert r.success and r.amount_out == 7
