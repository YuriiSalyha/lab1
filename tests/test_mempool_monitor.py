"""Unit tests for :mod:`pricing.mempool_monitor` and :mod:`pricing.parsed_swap` (no WebSocket)."""

from __future__ import annotations

from eth_abi import encode
from eth_utils import to_checksum_address

from chain.decoder import TransactionDecoder
from pricing.mempool_monitor import MempoolMonitor
from pricing.parsed_swap import try_parse_uniswap_v2_swap
from pricing.pricing_engine import DEFAULT_UNISWAP_V2_ROUTER

ROUTER = DEFAULT_UNISWAP_V2_ROUTER.checksum
FROM = "0x2222222222222222222222222222222222222222"
TOKEN_A = "0x1111111111111111111111111111111111111111"
TOKEN_B = "0x3333333333333333333333333333333333333333"
TX_HASH = "0x" + "ab" * 32


def _base_tx(*, input_data: bytes, value: int = 0) -> dict:
    return {
        "hash": TX_HASH,
        "from": FROM,
        "to": ROUTER,
        "input": input_data,
        "value": value,
        "maxFeePerGas": 30 * 10**9,
        "gasPrice": 0,
    }


def test_parse_swap_exact_tokens_for_tokens():
    path = [TOKEN_A, TOKEN_B]
    amount_in = 10**18
    amount_out_min = 9 * 10**17
    deadline = 1_700_000_000
    recipient = FROM
    body = encode(
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, amount_out_min, path, recipient, deadline],
    )
    calldata = bytes.fromhex("38ed1739") + body
    tx = _base_tx(input_data=calldata)

    mon = MempoolMonitor("ws://unused", lambda s: None)
    swap = mon.parse_transaction(tx)

    assert swap is not None
    assert swap.method == "swapExactTokensForTokens"
    assert swap.dex == "UniswapV2"
    assert swap.router == to_checksum_address(ROUTER)
    assert swap.sender.value == to_checksum_address(FROM)
    assert swap.amount_in == amount_in
    assert swap.min_amount_out == amount_out_min
    assert swap.deadline == deadline
    assert swap.token_in is not None and swap.token_in.value == to_checksum_address(TOKEN_A)
    assert swap.token_out is not None and swap.token_out.value == to_checksum_address(TOKEN_B)
    assert swap.gas_price == 30 * 10**9
    assert swap.tx_hash == TX_HASH
    assert swap.slippage_tolerance is None


def test_parse_swap_exact_eth_for_tokens():
    path = [TOKEN_A, TOKEN_B]
    amount_out_min = 5 * 10**17
    deadline = 1_700_000_001
    eth_in = 2 * 10**18
    body = encode(
        ["uint256", "address[]", "address", "uint256"],
        [amount_out_min, path, FROM, deadline],
    )
    calldata = bytes.fromhex("7ff36ab5") + body
    tx = _base_tx(input_data=calldata, value=eth_in)

    mon = MempoolMonitor("ws://unused", lambda s: None)
    swap = mon.parse_transaction(tx)

    assert swap is not None
    assert swap.method == "swapExactETHForTokens"
    assert swap.amount_in == eth_in
    assert swap.min_amount_out == amount_out_min


def test_parse_swap_eth_for_exact_tokens():
    path = [TOKEN_A, TOKEN_B]
    amount_out = 10**15
    deadline = 1_700_000_002
    max_eth = 10**18
    body = encode(
        ["uint256", "address[]", "address", "uint256"],
        [amount_out, path, FROM, deadline],
    )
    calldata = bytes.fromhex("fb3bdb41") + body
    tx = _base_tx(input_data=calldata, value=max_eth)

    mon = MempoolMonitor("ws://unused", lambda s: None)
    swap = mon.parse_transaction(tx)

    assert swap is not None
    assert swap.method == "swapETHForExactTokens"
    assert swap.amount_in == max_eth
    assert swap.min_amount_out == amount_out


def test_parse_non_swap_returns_none():
    transfer = bytes.fromhex("a9059cbb") + encode(["address", "uint256"], [TOKEN_B, 1])
    tx = _base_tx(input_data=transfer)
    mon = MempoolMonitor("ws://unused", lambda s: None)
    assert mon.parse_transaction(tx) is None


def test_try_parse_requires_path_length():
    decoded = TransactionDecoder.decode_function_call(bytes.fromhex("38ed1739"))
    assert decoded["params"] is None
    assert try_parse_uniswap_v2_swap({"from": FROM, "to": ROUTER, "hash": TX_HASH}, decoded) is None
