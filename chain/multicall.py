"""Multicall3 ``aggregate3`` — batch arbitrary ``eth_call`` payloads in one RPC."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web3 import Web3
from web3.contract import Contract

# Same on Ethereum mainnet and most EVM chains; override if needed.
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

_AGGREGATE3_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "bool", "name": "allowFailure", "type": "bool"},
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Call3[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"internalType": "bool", "name": "success", "type": "bool"},
                    {"internalType": "bytes", "name": "returnData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Result[]",
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]


@dataclass(frozen=True, slots=True)
class MulticallCall:
    target: str
    data: bytes
    allow_failure: bool = True


@dataclass(frozen=True, slots=True)
class MulticallResult:
    success: bool
    return_data: bytes


def _multicall_contract(w3: Web3) -> Contract:
    return w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=_AGGREGATE3_ABI,
    )


def aggregate3(
    w3: Web3,
    calls: list[MulticallCall],
    *,
    block_identifier: Any = "latest",
) -> list[MulticallResult]:
    """
    Execute batched static calls via Multicall3.

    Args:
        w3: Web3 instance (HTTP provider).
        calls: Each entry is one inner ``eth_call`` (target + calldata).
        block_identifier: Passed to ``eth_call``.
    """
    if not calls:
        return []
    c = _multicall_contract(w3)
    tuples = [(Web3.to_checksum_address(x.target), x.allow_failure, x.data) for x in calls]
    raw = c.functions.aggregate3(tuples).call(block_identifier=block_identifier)
    return [MulticallResult(success=bool(r[0]), return_data=bytes(r[1])) for r in raw]
