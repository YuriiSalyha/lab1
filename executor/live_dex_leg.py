"""
Live-chain Uniswap V2 router swap for one DEX arb leg (ERC20–ERC20 only).

Uses the same broadcast path as :func:`pricing.fork_swap_executor.broadcast_router_calldata`.
Caller must ensure router allowance and sufficient balances on the target chain.

A signed-but-not-broadcast variant powers ``ARB_DRY_RUN_MODE=signed`` so the
bot can run the full production pipeline (route + fork preflight + EIP-1559 build
+ signing) and only stop short of ``eth_sendRawTransaction``.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Callable, Optional

from chain.builder import TransactionBuilder
from chain.uniswap_v2_router import encode_uniswap_v2_swap_calldata
from core.types import Address, Token, TokenAmount
from core.wallet import WalletManager
from pricing.fork_simulator import ForkSimulator
from pricing.fork_swap_executor import broadcast_router_calldata
from pricing.pricing_engine import PricingEngine
from strategy.dex_token_resolver import find_pool_for_pair
from strategy.signal import Direction, Signal, to_decimal

logger = logging.getLogger(__name__)

MAINNET_CHAIN_ID = 1
BPS_DENOM = 10_000
DEADLINE_BUFFER_S = 30

# Synthetic tx hash prefix used in dry-run-signed responses so downstream consumers
# (CSV, Telegram, console) can recognise an "as-if-broadcast" tx at a glance.
DRY_RUN_TX_HASH_PREFIX = "0xDRYRUN"


class LiveDexLegError(Exception):
    """Raised when chain policy or routing blocks a live DEX leg."""


def _assert_dex_chain(
    chain_id: int,
    *,
    expected_chain_id: Optional[int],
    allow_mainnet: bool,
) -> None:
    if chain_id == MAINNET_CHAIN_ID and not allow_mainnet:
        raise LiveDexLegError(
            "refusing mainnet DEX execution: set dex_allow_mainnet=True and DEX_ALLOW_MAINNET=1",
        )
    if expected_chain_id is not None and chain_id != expected_chain_id:
        raise LiveDexLegError(
            f"chain id mismatch: connected {chain_id}, expected {expected_chain_id}",
        )


def dex_leg_buys_base(direction: Direction) -> bool:
    """True when the DEX leg acquires base with quote (see :mod:`executor.engine` leg ordering)."""
    return direction == Direction.BUY_DEX_SELL_CEX


def _amount_out_min_from_gross(gross_out: int, slippage_bps: Decimal) -> int:
    if gross_out <= 0:
        return 0
    mult = (Decimal(BPS_DENOM) - slippage_bps) / Decimal(BPS_DENOM)
    return max(0, int(Decimal(gross_out) * mult))


def _amount_in_max_with_slippage(amount_in: int, slippage_bps: Decimal) -> int:
    """Cap ``amountInMax`` for exact-out swaps with slippage tolerance on input."""
    if amount_in <= 0:
        return 0
    mult = (Decimal(BPS_DENOM) + slippage_bps) / Decimal(BPS_DENOM)
    return int(Decimal(amount_in) * mult) + 1


def _effective_price_quote_per_base(
    base_raw: int,
    quote_out_raw: int,
    base_decimals: int,
    quote_decimals: int,
) -> Decimal:
    b = Decimal(base_raw) / Decimal(10**base_decimals)
    q = Decimal(quote_out_raw) / Decimal(10**quote_decimals)
    if b <= 0:
        return Decimal("0")
    return q / b


def sync_execute_live_dex_leg(
    *,
    pricing_engine: PricingEngine,
    wallet: WalletManager,
    token_resolver: Callable[[str], tuple[Token, Token]],
    signal: Signal,
    size_base_human: Decimal,
    direction: Direction,
    slippage_bps: Decimal,
    deadline_seconds: int,
    run_preflight: bool,
    expected_chain_id: Optional[int],
    allow_mainnet: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Execute one V2 router swap for the DEX leg.

    When ``dry_run`` is ``True`` the function still runs the **full production
    flow** — route resolution, optional fork preflight, EIP-1559 transaction
    build, balance check, and signing — but skips ``eth_sendRawTransaction``.
    The returned dict includes the signed raw-tx hex under ``signed_raw_tx_hex``
    plus a synthetic ``tx_hash`` starting with ``0xDRYRUN`` so callers can treat
    the result like a successful broadcast for accounting / notifications.

    Returns the same shape as :meth:`executor.engine.Executor._execute_dex_leg` simulation dict.
    """
    if pricing_engine.route_finder is None or not pricing_engine.pools:
        return {
            "success": False,
            "price": signal.dex_price,
            "filled": Decimal("0"),
            "error": "pricing_engine_not_ready",
            "dry_run": dry_run,
        }

    client = pricing_engine.client
    chain_id = int(client.w3.eth.chain_id)
    try:
        _assert_dex_chain(
            chain_id, expected_chain_id=expected_chain_id, allow_mainnet=allow_mainnet
        )
    except LiveDexLegError as e:
        return {
            "success": False,
            "price": signal.dex_price,
            "filled": Decimal("0"),
            "error": str(e),
            "dry_run": dry_run,
        }

    base_t, quote_t = token_resolver(signal.pair)
    base_raw = int(TokenAmount.from_human(size_base_human, base_t.decimals, base_t.symbol).raw)
    if base_raw <= 0:
        return {
            "success": False,
            "price": signal.dex_price,
            "filled": Decimal("0"),
            "error": "zero_base_amount",
            "dry_run": dry_run,
        }

    router = pricing_engine.swap_router
    sender = Address.from_string(wallet.address)
    deadline = int(time.time()) + int(deadline_seconds) + DEADLINE_BUFFER_S
    rpc_url = client.rpc_urls[0]
    buys = dex_leg_buys_base(direction)
    parts = signal.pair.strip().upper().split("/")
    base_sym, quote_sym = parts[0], parts[1]
    quote_atoms_for_price = 0

    try:
        if buys:
            pool = find_pool_for_pair(pricing_engine.pools, base_sym, quote_sym)
            quote_in_needed = pool.get_amount_in(base_raw, base_t)
            quote_atoms_for_price = quote_in_needed
            amount_in_max = _amount_in_max_with_slippage(quote_in_needed, slippage_bps)
            path_tokens = [quote_t.address, base_t.address]
            calldata = encode_uniswap_v2_swap_calldata(
                "swapTokensForExactTokens",
                path=path_tokens,
                to=sender,
                deadline=deadline,
                amount_out=base_raw,
                amount_in_max=amount_in_max,
            )
            swap_params: dict[str, Any] = {
                "function": "swapTokensForExactTokens",
                "amount_out": base_raw,
                "amount_in_max": amount_in_max,
                "path": path_tokens,
                "to": sender,
                "deadline": deadline,
            }
        else:
            rf = pricing_engine.route_finder
            route, _net = rf.find_best_route(base_t, quote_t, base_raw, 0, max_hops=3)
            if route is None:
                return {
                    "success": False,
                    "price": signal.dex_price,
                    "filled": Decimal("0"),
                    "error": "no_route",
                    "dry_run": dry_run,
                }
            gross = route.get_output(base_raw)
            quote_atoms_for_price = gross
            amount_out_min = _amount_out_min_from_gross(gross, slippage_bps)
            path_tokens = [t.address for t in route.path]
            calldata = encode_uniswap_v2_swap_calldata(
                "swapExactTokensForTokens",
                path=path_tokens,
                to=sender,
                deadline=deadline,
                amount_in=base_raw,
                amount_out_min=amount_out_min,
            )
            swap_params = {
                "function": "swapExactTokensForTokens",
                "amount_in": base_raw,
                "amount_out_min": amount_out_min,
                "path": path_tokens,
                "to": sender,
                "deadline": deadline,
            }

        preflight_gas_used: Optional[int] = None
        if run_preflight:
            sim = ForkSimulator(rpc_url).simulate_swap(router, swap_params, sender)
            if not sim.success:
                return {
                    "success": False,
                    "price": signal.dex_price,
                    "filled": Decimal("0"),
                    "error": f"preflight:{sim.error}",
                    "dry_run": dry_run,
                }
            preflight_gas_used = int(getattr(sim, "gas_used", 0) or 0)

        price = _effective_price_quote_per_base(
            base_raw,
            quote_atoms_for_price,
            base_t.decimals,
            quote_t.decimals,
        )

        if dry_run:
            # Build + sign the EIP-1559 transaction so it is byte-identical to
            # what we would have broadcast — only `eth_sendRawTransaction` is
            # skipped. The signed payload is exposed in the returned dict so the
            # bot can log it / Telegram it without ever touching the network.
            try:
                builder = TransactionBuilder(client, wallet)
                builder.to(router)
                builder.data(calldata)
                builder.value(TokenAmount(raw=0, decimals=18))
                builder.with_gas_estimate()
                builder.with_gas_price()
                signed = builder.build_and_sign()
            except Exception as build_exc:
                logger.warning("dry-run signed build failed: %s", build_exc)
                return {
                    "success": False,
                    "price": signal.dex_price,
                    "filled": Decimal("0"),
                    "error": f"dry_run_build:{build_exc}",
                    "dry_run": True,
                }

            raw_tx_bytes = bytes(signed.raw_transaction)
            raw_tx_hex = "0x" + raw_tx_bytes.hex()
            real_hash = signed.hash.hex() if hasattr(signed.hash, "hex") else str(signed.hash)
            if not real_hash.startswith("0x"):
                real_hash = "0x" + real_hash
            synthetic_hash = f"{DRY_RUN_TX_HASH_PREFIX}{real_hash[2:18]}"
            logger.info(
                "DEX leg dry-run signed tx (NOT broadcast) real_hash_prefix=%s synthetic=%s",
                real_hash[:12],
                synthetic_hash,
            )
            return {
                "success": True,
                "price": to_decimal(price),
                "filled": to_decimal(size_base_human),
                "tx_hash": synthetic_hash,
                "dry_run": True,
                "signed_raw_tx_hex": raw_tx_hex,
                "signed_tx_hash": real_hash,
                "preflight_gas_used": preflight_gas_used,
                "router": router.checksum,
                "swap_params": swap_params,
            }

        tx_hash, _receipt, _parsed = broadcast_router_calldata(client, wallet, router, calldata)
        logger.info("live DEX leg mined tx=%s", tx_hash[:18])

        return {
            "success": True,
            "price": to_decimal(price),
            "filled": to_decimal(size_base_human),
            "tx_hash": tx_hash,
            "dry_run": False,
        }
    except Exception as e:
        logger.exception("live DEX leg failed: %s", e)
        return {
            "success": False,
            "price": signal.dex_price,
            "filled": Decimal("0"),
            "error": str(e),
            "dry_run": dry_run,
        }
