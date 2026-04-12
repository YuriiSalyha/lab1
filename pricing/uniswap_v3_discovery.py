"""Resolve Uniswap V3 pool addresses via the canonical factory (no subgraph)."""

from __future__ import annotations

from typing import Any

from web3 import Web3

# Ethereum mainnet — https://docs.uniswap.org/contracts/v3/reference/deployments/ethereum-deployments
V3_FACTORY_MAINNET = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

DEFAULT_FEE_TIERS: tuple[int, ...] = (500, 3000, 10_000)

_FACTORY_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint24", "name": "", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _factory_contract(w3: Web3, factory_address: str) -> Any:
    return w3.eth.contract(
        address=Web3.to_checksum_address(factory_address),
        abi=_FACTORY_ABI,
    )


def get_pool_address(
    w3: Web3,
    token_a: str,
    token_b: str,
    fee: int,
    *,
    factory_address: str | None = None,
) -> str | None:
    """
    Return checksummed pool address, or ``None`` if the pool is not deployed.

    ``token_a`` / ``token_b`` may be in either order (factory sorts them).
    """
    fa = Web3.to_checksum_address(factory_address or V3_FACTORY_MAINNET)
    t0 = Web3.to_checksum_address(token_a)
    t1 = Web3.to_checksum_address(token_b)
    c = _factory_contract(w3, fa)
    pool = c.functions.getPool(t0, t1, int(fee)).call()
    if not pool or pool == "0x0000000000000000000000000000000000000000":
        return None
    return Web3.to_checksum_address(pool)


def pools_for_pair(
    w3: Web3,
    token_a: str,
    token_b: str,
    *,
    fee_tiers: tuple[int, ...] | None = None,
    factory_address: str | None = None,
) -> list[tuple[int, str]]:
    """For each fee tier, return ``(fee, pool_checksum)`` when a pool exists."""
    tiers = fee_tiers or DEFAULT_FEE_TIERS
    out: list[tuple[int, str]] = []
    for fee in tiers:
        addr = get_pool_address(w3, token_a, token_b, fee, factory_address=factory_address)
        if addr is not None:
            out.append((fee, addr))
    return out
