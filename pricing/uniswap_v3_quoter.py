"""Uniswap V3 QuoterV2 calldata encode/decode (mainnet deployment)."""

from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode
from web3 import Web3
from web3.contract import Contract

from pricing.liquidity_pool import QuoteResult

# Ethereum mainnet — see https://docs.uniswap.org/contracts/v3/reference/deployments/ethereum-deployments
QUOTER_V2_MAINNET = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

_QUOTER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_POOL_ABI: list[dict[str, Any]] = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"type": "uint24"}], "type": "function"},
]


def quoter_contract(w3: Web3, quoter_address: str | None = None) -> Contract:
    addr = quoter_address or QUOTER_V2_MAINNET
    return w3.eth.contract(address=Web3.to_checksum_address(addr), abi=_QUOTER_ABI)


def encode_quote_exact_input_single(
    w3: Web3,
    *,
    token_in: str,
    token_out: str,
    fee: int,
    amount_in: int,
    sqrt_price_limit_x96: int = 0,
    quoter_address: str | None = None,
) -> bytes:
    """ABI-encoded calldata for ``quoteExactInputSingle`` (for Multicall or ``eth_call``)."""
    c = quoter_contract(w3, quoter_address)
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        amount_in,
        fee,
        sqrt_price_limit_x96,
    )
    data = c.functions.quoteExactInputSingle(params)._encode_transaction_data()
    if isinstance(data, str):
        h = data[2:] if data.startswith("0x") else data
        return bytes.fromhex(h)
    return bytes(data)


def decode_quote_exact_input_single_return(return_data: bytes) -> QuoteResult:
    """Decode QuoterV2 return tuple to :class:`QuoteResult`."""
    amount_out, _sqrt, _ticks, gas_est = abi_decode(
        ["uint256", "uint160", "uint32", "uint256"],
        return_data,
    )
    return QuoteResult(amount_out=int(amount_out), gas_estimate=int(gas_est))


def read_v3_pool_meta(w3: Web3, pool_address: str) -> tuple[str, str, int]:
    """Return checksummed (token0, token1, fee) from a V3 pool contract."""
    p = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=_POOL_ABI)
    t0 = p.functions.token0().call()
    t1 = p.functions.token1().call()
    fee = int(p.functions.fee().call())
    return Web3.to_checksum_address(t0), Web3.to_checksum_address(t1), fee
