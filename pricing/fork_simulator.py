"""Simulate Uniswap V2–style router swaps on a fork via ``eth_call``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from chain.decoder import TransactionDecoder
from chain.uniswap_v2_router import (
    UNISWAP_V2_SWAP_FUNCTION_NAMES,
    decode_swap_amounts_return_data,
    encode_uniswap_v2_swap_calldata,
)
from core.types import Address, Token
from pricing.route import Route
from pricing.uniswap_v2_pair import UniswapV2Pair

_DEFAULT_CALL_GAS = 500_000
_MAX_DEADLINE = 2**256 - 1


def _format_simulation_error(err: BaseException) -> str:
    if isinstance(err, ContractLogicError):
        msg = err.message
        if isinstance(msg, str):
            msg = msg.strip() or None
        else:
            msg = None
        data = err.data
        decoded: Optional[str] = None
        if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
            decoded = TransactionDecoder.decode_revert_reason(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            decoded = TransactionDecoder.decode_revert_reason(bytes(data))
        if decoded:
            if msg and decoded in msg:
                return msg
            return decoded
        return msg or str(err)
    return str(err)


def _coerce_bytes_data(data: Any) -> bytes:
    if data is None:
        raise ValueError("swap_params['data'] is None")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        h = data[2:] if data.startswith("0x") else data
        return bytes.fromhex(h)
    if hasattr(data, "hex"):
        hx = data.hex()
        if hx.startswith("0x"):
            hx = hx[2:]
        return bytes.fromhex(hx)
    raise TypeError(f"swap_params['data'] must be bytes-like, got {type(data)}")


def _build_calldata_from_swap_params(swap_params: dict) -> bytes:
    if "data" in swap_params and swap_params["data"] is not None:
        return _coerce_bytes_data(swap_params["data"])

    fn = swap_params.get("function") or swap_params.get("function_name")
    if not fn or fn not in UNISWAP_V2_SWAP_FUNCTION_NAMES:
        raise ValueError(
            "swap_params must include 'data' or a supported 'function' "
            f"(one of {sorted(UNISWAP_V2_SWAP_FUNCTION_NAMES)})"
        )

    path = swap_params.get("path")
    if not path:
        raise ValueError("structured swap_params requires 'path'")
    to = swap_params.get("to")
    if to is None:
        raise ValueError("structured swap_params requires 'to'")
    if "deadline" not in swap_params:
        raise ValueError("structured swap_params requires 'deadline'")

    deadline = int(swap_params["deadline"])
    amount_in = swap_params.get("amount_in", swap_params.get("amountIn"))
    amount_out_min = swap_params.get("amount_out_min", swap_params.get("amountOutMin"))
    amount_out = swap_params.get("amount_out", swap_params.get("amountOut"))
    amount_in_max = swap_params.get("amount_in_max", swap_params.get("amountInMax"))

    return encode_uniswap_v2_swap_calldata(
        fn,
        path=path,
        to=to,
        deadline=deadline,
        amount_in=int(amount_in) if amount_in is not None else None,
        amount_out_min=int(amount_out_min) if amount_out_min is not None else None,
        amount_out=int(amount_out) if amount_out is not None else None,
        amount_in_max=int(amount_in_max) if amount_in_max is not None else None,
    )


@dataclass
class SimulationResult:
    success: bool
    amount_out: int
    gas_used: int
    error: Optional[str]
    # ``eth_call`` does not return logs; reserved for future trace-based simulation.
    logs: list[Any]


class ForkSimulator:
    """Simulates router swaps on a local Anvil/Hardhat fork using ``eth_call``."""

    def __init__(self, fork_url: str) -> None:
        self.w3 = Web3(Web3.HTTPProvider(fork_url))

    def simulate_swap(
        self,
        router: Address,
        swap_params: dict,
        sender: Address,
    ) -> SimulationResult:
        """
        Simulate a router swap. ``swap_params`` either:

        - ``{"data": bytes, "value"?: int, "gas"?: int}``, or
        - structured fields: ``function``, ``path``, ``to``, ``deadline``, and
          amounts named ``amount_in`` / ``amount_out_min`` / etc. (or Solidity
          camelCase aliases).

        The ``sender`` must have token balance and router allowance on the fork
        (or use Anvil impersonation in tests).

        Standard ERC-20 pairs match :meth:`UniswapV2Pair.get_amount_out`; fee-on-transfer
        or rebasing tokens can diverge.
        """
        try:
            data = _build_calldata_from_swap_params(swap_params)
        except (ValueError, TypeError) as e:
            return SimulationResult(
                success=False,
                amount_out=0,
                gas_used=0,
                error=str(e),
                logs=[],
            )

        value = int(swap_params.get("value", 0))
        gas = int(swap_params.get("gas", _DEFAULT_CALL_GAS))

        tx: dict[str, Any] = {
            "from": sender.checksum,
            "to": router.checksum,
            "data": data,
            "value": value,
            "gas": gas,
        }

        try:
            ret = self.w3.eth.call(tx)
            amounts = decode_swap_amounts_return_data(ret)
            amount_out = int(amounts[-1]) if amounts else 0
        except ContractLogicError as e:
            return SimulationResult(
                success=False,
                amount_out=0,
                gas_used=0,
                error=_format_simulation_error(e),
                logs=[],
            )
        except Exception as e:
            return SimulationResult(
                success=False,
                amount_out=0,
                gas_used=0,
                error=str(e),
                logs=[],
            )

        gas_used = 0
        try:
            gas_used = int(self.w3.eth.estimate_gas(tx))
        except Exception:
            pass

        return SimulationResult(
            success=True,
            amount_out=amount_out,
            gas_used=gas_used,
            error=None,
            logs=[],
        )

    def simulate_route(
        self,
        router: Address,
        route: Route,
        amount_in: int,
        sender: Address,
        deadline: int,
        *,
        recipient: Optional[Address] = None,
        amount_out_min: int = 0,
    ) -> SimulationResult:
        """
        Multi-hop ``swapExactTokensForTokens`` in one router call.

        If ``estimate_gas`` fails after a successful ``eth_call``, ``gas_used`` falls
        back to :meth:`Route.estimate_gas`.
        """
        to_addr = recipient if recipient is not None else sender
        swap_params = {
            "function": "swapExactTokensForTokens",
            "amount_in": amount_in,
            "amount_out_min": amount_out_min,
            "path": [t.address for t in route.path],
            "to": to_addr,
            "deadline": deadline,
        }
        res = self.simulate_swap(router, swap_params, sender)
        if res.success and res.gas_used == 0:
            return SimulationResult(
                success=res.success,
                amount_out=res.amount_out,
                gas_used=route.estimate_gas(),
                error=res.error,
                logs=res.logs,
            )
        return res

    def compare_simulation_vs_calculation(
        self,
        router: Address,
        pair: UniswapV2Pair,
        amount_in: int,
        token_in: Token,
        sender: Address,
        *,
        recipient: Optional[Address] = None,
        deadline: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Compare :meth:`~UniswapV2Pair.get_amount_out` to a single-hop
        ``swapExactTokensForTokens`` ``eth_call`` on the fork.

        ``match`` is true only when simulation succeeds and the simulated output
        equals the closed-form AMM result (not guaranteed for fee-on-transfer tokens).

        Returns:
            Dict with keys ``calculated``, ``simulated``, ``difference``, ``match``,
            and ``error`` (simulation error message, or ``None`` on success).
        """
        if token_in not in (pair.token0, pair.token1):
            raise ValueError(f"token_in {token_in!r} is not a token on this pair")
        token_out = pair.token1 if token_in == pair.token0 else pair.token0
        calculated = pair.get_amount_out(amount_in, token_in)
        dl = _MAX_DEADLINE if deadline is None else int(deadline)
        to_addr = recipient if recipient is not None else sender

        sim = self.simulate_swap(
            router,
            {
                "function": "swapExactTokensForTokens",
                "amount_in": amount_in,
                "amount_out_min": 0,
                "path": [token_in.address, token_out.address],
                "to": to_addr,
                "deadline": dl,
            },
            sender,
        )
        sim_amt = sim.amount_out if sim.success else 0
        return {
            "calculated": calculated,
            "simulated": sim_amt,
            "difference": abs(calculated - sim_amt),
            "match": sim.success and calculated == sim_amt,
            "error": sim.error,
        }
