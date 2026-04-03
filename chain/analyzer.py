"""Transaction Analyzer CLI.

Usage::

    python -m chain.analyzer TX_HASH [--rpc URL]

Environment:
    ``RPC_ENDPOINT`` — primary RPC endpoint.
    ``SEPOLIA_RPC`` — optional fallback RPC when the tx is not found on the primary
    endpoint.
    ``LOG_FILE`` — optional path for log file (default: ``logs/lab1.log``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from eth_utils import to_checksum_address
from web3 import Web3

from chain.client import ChainClient
from chain.decoder import TransactionDecoder
from chain.errors import InvalidParameterError
from chain.helpers import format_human_token_amount, token_symbol_and_decimals
from chain.validation import normalize_tx_hash
from core.logging_config import configure_project_logging

logger = logging.getLogger(__name__)

_DEFAULT_PUBLIC_MAINNET_RPC = "https://eth.llamarpc.com"


def _default_mainnet_rpc_url() -> str:
    """Primary RPC endpoint (tx hashes are expected here first)."""
    return os.getenv("RPC_ENDPOINT") or _DEFAULT_PUBLIC_MAINNET_RPC


def _default_testnet_rpc_urls() -> list[str]:
    """Candidate RPCs for fallback when the tx isn't found on the primary RPC."""
    val = os.getenv("SEPOLIA_RPC")
    return [val] if val else []


def _looks_like_tx_not_found(err: Exception) -> bool:
    """Heuristic to decide whether we should try a different network."""
    text = str(err).lower()
    # Common geth/web3 responses:
    # - "Transaction with hash ... not found"
    # - "Transaction not found: ..."
    # - "eth_getTransactionByHash" not found (varies by client/provider)
    return "not found" in text or "transaction not found" in text


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------


def _fmt_number(n: int) -> str:
    """Format an int with thousands separators."""
    return f"{n:,}"


def _wei_to_gwei(wei: int) -> Decimal:
    return Decimal(wei) / Decimal(10**9)


def _wei_to_eth(wei: int) -> Decimal:
    return Decimal(wei) / Decimal(10**18)


def _fmt_gwei(wei: int) -> str:
    return f"{_wei_to_gwei(wei):.1f} gwei"


def _fmt_eth(wei: int) -> str:
    return f"{_wei_to_eth(wei):.6f} ETH"


def _short_addr(addr: str | None, n: int = 6) -> str:
    """Truncate a long hex address for narrow columns."""
    if not addr:
        return "?"
    if len(addr) <= n * 2 + 2:
        return addr
    return f"{addr[: n + 2]}...{addr[-n:]}"


def _fmt_uint_with_token(raw: int, decimals: int, symbol: str) -> str:
    """``raw`` atomic units plus human-readable suffix in parentheses."""
    human = Decimal(raw) / Decimal(10**decimals)
    return f"{raw:,} ({human:,.4f} {symbol})"


def _fmt_bytes_compact(b: bytes) -> str:
    """Hex preview for calldata / ABI ``bytes`` values (avoid huge ``repr`` dumps)."""
    n = len(b)
    if n == 0:
        return "0x"
    if n <= 48:
        return "0x" + b.hex()
    return f"0x{b[:20].hex()}…{b[-8:].hex()} ({n} bytes)"


def _format_abi_value_preview(value: object, *, _depth: int = 0) -> str:
    """Format ABI-decoded values (bytes, nested tuples) for one-line CLI output."""
    if _depth > 8:
        return "…"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _fmt_bytes_compact(bytes(value))
    if isinstance(value, str):
        if len(value) > 160:
            return value[:157] + "…"
        return value
    if isinstance(value, tuple):
        inner = ", ".join(_format_abi_value_preview(x, _depth=_depth + 1) for x in value)
        return f"({inner})"
    if isinstance(value, list):
        inner = ", ".join(_format_abi_value_preview(x, _depth=_depth + 1) for x in value)
        return f"[{inner}]"
    return str(value)


def _fmt_deadline(ts: int) -> str:
    """Unix timestamp plus UTC datetime in parentheses."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{ts} ({dt.strftime('%Y-%m-%d %H:%M:%S UTC')})"


def _fmt_path_symbols(path: list[str], client: ChainClient) -> str:
    """``[SYM, SYM, ...]`` using token metadata for each hop."""
    labels = [token_symbol_and_decimals(client, a)[0] for a in path]
    return "[" + ", ".join(labels) + "]"


def decode_uniswap_v3_path(path_bytes: bytes) -> list[str]:
    """Decode Uniswap V3 ``path`` bytes: ``[token][fee][token][fee]…[token]``."""
    if not path_bytes:
        return []
    tokens: list[str] = []
    i = 0
    while i < len(path_bytes):
        if i + 20 > len(path_bytes):
            break
        chunk = path_bytes[i : i + 20]
        tokens.append(to_checksum_address("0x" + chunk.hex()))
        i += 20
        if i < len(path_bytes):
            if i + 3 > len(path_bytes):
                break
            i += 3
    return tokens


def _format_arg_value(
    name: str,
    value: object,
    func_name: str,
    params: dict[str, object],
    tx: dict,
    client: ChainClient,
) -> str:
    """Pretty-print one decoded argument (amounts + token symbols, path, deadline).

    Args:
        name: Parameter name from the ABI.
        value: Decoded value.
        func_name: Short function name (e.g. ``swapExactTokensForTokens``).
        params: Full decoded parameter dict (for ``path``, token addresses).
        tx: Raw tx dict (``to`` is the token for ERC-20 calls).
        client: For ``token_cache`` lookups.

    Returns:
        Single-line string for CLI output.
    """
    token_contract = tx.get("to")
    path = params.get("path")
    path_list = path if isinstance(path, list) else None

    if name == "deadline" and isinstance(value, int):
        return _fmt_deadline(value)

    if (
        name == "path"
        and isinstance(value, (bytes, bytearray, memoryview))
        and func_name == "exactInput"
    ):
        tokens = decode_uniswap_v3_path(bytes(value))
        if tokens:
            return _fmt_path_symbols(tokens, client)
        return _fmt_bytes_compact(bytes(value))

    if name == "path" and isinstance(value, list) and value:
        return _fmt_path_symbols(value, client)

    if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
        if name in ("to", "from", "spender", "account", "tokenA", "tokenB", "token"):
            return f"{value} ({_short_addr(value)})"

    if isinstance(value, bool) or not isinstance(value, int):
        return _format_abi_value_preview(value)

    # ERC-20: value field uses the token contract at tx["to"]
    if func_name in ("transfer", "approve", "transferFrom") and name == "value" and token_contract:
        sym, dec = token_symbol_and_decimals(client, token_contract)
        return _fmt_uint_with_token(value, dec, sym)

    # ------------------------------------------------------------------
    # Uniswap V2-style swaps — map amount fields to `path` indices
    # ------------------------------------------------------------------
    swap_param_to_path_index: dict[str, dict[str, int]] = {
        "swapExactTokensForTokens": {"amountIn": 0, "amountOutMin": -1},
        "swapExactTokensForETH": {"amountIn": 0, "amountOutMin": -1},
        "swapExactETHForTokens": {"amountOutMin": -1},
        "swapETHForExactTokens": {"amountOut": -1},
        "swapTokensForExactTokens": {"amountOut": -1, "amountInMax": 0},
        "swapTokensForExactETH": {"amountOut": -1, "amountInMax": 0},
    }
    if path_list:
        idx = swap_param_to_path_index.get(func_name, {}).get(name)
        if idx is not None:
            sym, dec = token_symbol_and_decimals(client, path_list[idx])
            return _fmt_uint_with_token(value, dec, sym)

    # ------------------------------------------------------------------
    # Liquidity ops — map amount fields to token params / native ETH
    # ------------------------------------------------------------------
    liquidity_token_param_by_name: dict[str, dict[str, str]] = {
        "addLiquidity": {
            "amountADesired": "tokenA",
            "amountAMin": "tokenA",
            "amountBDesired": "tokenB",
            "amountBMin": "tokenB",
        },
        "removeLiquidity": {
            "amountAMin": "tokenA",
            "amountBMin": "tokenB",
        },
        "addLiquidityETH": {
            "amountTokenDesired": "token",
            "amountTokenMin": "token",
        },
        "removeLiquidityETH": {
            "amountTokenMin": "token",
        },
    }
    token_param_key = liquidity_token_param_by_name.get(func_name, {}).get(name)
    if token_param_key:
        token_addr = params.get(token_param_key)
        if token_addr:
            a = str(token_addr)
            sym, dec = token_symbol_and_decimals(client, a)
            return _fmt_uint_with_token(value, dec, sym)

    liquidity_eth_param_names: dict[str, set[str]] = {
        "addLiquidityETH": {"amountETHMin"},
        "removeLiquidityETH": {"amountETHMin"},
    }
    if name in liquidity_eth_param_names.get(func_name, set()):
        return _fmt_uint_with_token(value, 18, "ETH")

    return f"{value:,}"


def _inner_call_headline(inner: dict) -> str:
    """One-line label for a nested calldata chunk."""
    if inner.get("function") == "unknown":
        sel = inner.get("selector") or ""
        return f"unknown(0x{sel})"
    sig = inner.get("signature")
    func = inner.get("function")
    if sig and sig != func:
        return str(sig)
    return f"{func}(...)" if func else "unknown(...)"


def _print_internal_calls(calls: list[object], client: ChainClient, tx: dict) -> None:
    """Decode each ``bytes`` in a Uniswap-style ``multicall`` batch and print details."""
    print()
    print("Internal Calls")
    print("-" * 40)
    for i, raw in enumerate(calls, 1):
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            print(f"{i}. (not calldata bytes)")
            continue
        inner = TransactionDecoder.decode_function_call(bytes(raw))
        print(f"{i}. {_inner_call_headline(inner)}")
        inner_func = inner.get("function") or "unknown"
        inner_params = inner.get("params")
        if not inner_params or inner_func == "unknown":
            continue
        order = inner.get("param_names") or list(inner_params.keys())
        for pname in order:
            if pname not in inner_params:
                continue
            val = inner_params[pname]
            line = _format_arg_value(pname, val, inner_func, inner_params, tx, client)
            print(f"     - {pname + ':':<16} {line}")


def _print_function(decoded: dict, client: ChainClient, tx: dict) -> None:
    """Print Function Called section (signature + formatted arguments)."""
    print()
    print("Function Called")
    print("-" * 40)
    print(f"{'Selector:':<20}0x{decoded['selector']}")

    func = decoded["function"]
    sig = decoded.get("signature")
    if func == "unknown":
        print(f"{'Function:':<20}Unknown (0x{decoded['selector']})")
    elif sig:
        print(f"{'Function:':<20}{sig}")
    else:
        print(f"{'Function:':<20}{func}")

    params = decoded.get("params")
    if params is None:
        if func != "unknown":
            print("  (complex / struct arguments — raw data omitted)")
        return

    if not params:
        return

    if func == "multicall":
        inner_list = params.get("data")
        if isinstance(inner_list, list) and inner_list:
            order = decoded.get("param_names") or list(params.keys())
            print("Arguments:")
            for pname in order:
                if pname not in params:
                    continue
                val = params[pname]
                if pname == "data" and isinstance(val, list):
                    print(f"  - {pname + ':':<18} {len(val)} sub-call(s) — see Internal Calls")
                else:
                    line = _format_arg_value(pname, val, func, params, tx, client)
                    print(f"  - {pname + ':':<18} {line}")
            _print_internal_calls(inner_list, client, tx)
            return

    order = decoded.get("param_names") or list(params.keys())
    print("Arguments:")
    for pname in order:
        if pname not in params:
            continue
        val = params[pname]
        line = _format_arg_value(pname, val, func, params, tx, client)
        print(f"  - {pname + ':':<18} {line}")


# ------------------------------------------------------------------
# Section printers
# ------------------------------------------------------------------


def _print_header(tx_hash: str, tx: dict, receipt, block, w3: Web3) -> None:
    """Print summary block (hash, block, time, status, from/to/value)."""
    print()
    print("Transaction Analysis")
    print("=" * 100)
    print(f"{'Hash:':<20}{tx_hash}")

    if block is not None:
        print(f"{'Block:':<20}{_fmt_number(block['number'])}")
        ts = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)
        print(f"{'Timestamp:':<20}{ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if receipt is None:
        print(f"{'Status:':<20}PENDING")
    elif receipt.status:
        print(f"{'Status:':<20}SUCCESS")
    else:
        print(f"{'Status:':<20}REVERTED")

    print()
    print(f"{'From:':<20}{tx['from']}")
    to_addr = tx.get("to") or "Contract Creation"
    print(f"{'To:':<20}{to_addr}")
    print(f"{'Value:':<20}{_fmt_eth(tx.get('value', 0))}")


def _print_gas_analysis(tx: dict, receipt, block) -> None:
    """Print EIP-1559 fee breakdown and total fee in ETH."""
    gas_limit = tx.get("gas", 0)
    gas_used = receipt.gas_used
    effective_price = receipt.effective_gas_price
    base_fee = block.get("baseFeePerGas", 0)
    priority_fee = max(effective_price - base_fee, 0)
    tx_fee_wei = gas_used * effective_price
    efficiency = (gas_used / gas_limit * 100) if gas_limit else 0

    print()
    print("Gas Analysis")
    print("-" * 40)
    print(f"{'Gas Limit:':<20}{_fmt_number(gas_limit)}")
    print(f"{'Gas Used:':<20}{_fmt_number(gas_used)} ({efficiency:.2f}%)")
    print(f"{'Base Fee:':<20}{_fmt_gwei(base_fee)}")
    print(f"{'Priority Fee:':<20}{_fmt_gwei(priority_fee)}")
    print(f"{'Effective Price:':<20}{_fmt_gwei(effective_price)}")
    print(f"{'Transaction Fee:':<20}{_fmt_eth(tx_fee_wei)}")


def _print_transfers(events: list[dict], client: ChainClient) -> None:
    """Print ERC-20 Transfer events with resolved symbols."""
    transfers = [e for e in events if e["name"] == "Transfer"]
    if not transfers:
        return

    print()
    print("Token Transfers")
    print("-" * 40)

    for i, t in enumerate(transfers, 1):
        d = t.get("decoded") or {}
        token_addr = t.get("address", "")
        sym, dec = token_symbol_and_decimals(client, token_addr)
        raw_value = d.get("value")
        amount_str = format_human_token_amount(raw_value, dec, sym)

        from_addr = _short_addr(d.get("from"))
        to_addr = _short_addr(d.get("to"))
        print(f"{i:>2}. {sym + ':':<6}  {from_addr} → {to_addr}     {amount_str}")


def _print_swap_summary(tx: dict, events: list[dict], client: ChainClient) -> None:
    """Aggregate net token flows to/from the tx sender (Transfer-based heuristic)."""
    transfers = [e for e in events if e["name"] == "Transfer"]
    if not transfers:
        return

    sender = tx["from"].lower()
    sold: list[dict] = []
    received: list[dict] = []

    for t in transfers:
        d = t.get("decoded") or {}
        from_addr = (d.get("from") or "").lower()
        to_addr = (d.get("to") or "").lower()
        token_addr = t.get("address", "")
        raw_value = d.get("value", 0)

        sym, dec = token_symbol_and_decimals(client, token_addr)
        human = Decimal(raw_value) / Decimal(10**dec) if raw_value else Decimal(0)
        entry = {"symbol": sym, "amount": human}

        if from_addr == sender:
            sold.append(entry)
        if to_addr == sender:
            received.append(entry)

    if not sold and not received:
        return

    sold_tot: defaultdict[str, Decimal] = defaultdict(Decimal)
    recv_tot: defaultdict[str, Decimal] = defaultdict(Decimal)
    for s in sold:
        sold_tot[s["symbol"]] += s["amount"]
    for r in received:
        recv_tot[r["symbol"]] += r["amount"]

    print()
    print("Swap Summary")
    print("-" * 40)
    if sold_tot:
        sold_parts = [
            f"{amt:,.4f} {sym}"
            for sym, amt in sorted(sold_tot.items(), key=lambda x: (-x[1], x[0]))
        ]
        print(f"  {'Sold:':<20}{', '.join(sold_parts)}")
    if recv_tot:
        recv_parts = [
            f"{amt:,.4f} {sym}"
            for sym, amt in sorted(recv_tot.items(), key=lambda x: (-x[1], x[0]))
        ]
        print(f"  {'Received:':<20}{', '.join(recv_parts)}")

    if len(sold_tot) == 1 and len(recv_tot) == 1:
        (s_sym, s_amt) = next(iter(sold_tot.items()))
        (r_sym, r_amt) = next(iter(recv_tot.items()))
        if r_amt > 0:
            price = s_amt / r_amt
            print(f"  {'Execution Price:':<20}{price:,.2f} {s_sym}/{r_sym}")


def _print_revert_info(tx_hash: str, client: ChainClient) -> None:
    """Print best-effort revert reason for failed txs."""
    print()
    print("Revert Info")
    print("-" * 40)
    reason = client.get_revert_reason(tx_hash)
    print(f"  Reason: {reason or 'Could not determine revert reason'}")


# ------------------------------------------------------------------
# Main analysis entry point
# ------------------------------------------------------------------


def analyze(tx_hash: str, mainnet_rpc_url: str, testnet_rpc_urls: list[str] | None) -> None:
    """Fetch tx/receipt and print all sections to stdout.

    Args:
        tx_hash: Full 32-byte transaction hash (hex).
        mainnet_rpc_url: Mainnet RPC endpoint (tx hashes are expected here first).
        testnet_rpc_urls: Testnet RPC endpoints to try if the tx is not found on mainnet.
    """
    logger.info("analyze start: hash_prefix=%s", tx_hash[:12])
    rpc_candidates: list[tuple[str, str]] = [("mainnet", mainnet_rpc_url)]
    if testnet_rpc_urls:
        for u in testnet_rpc_urls:
            if u and u != mainnet_rpc_url:
                rpc_candidates.append(("testnet", u))

    last_error: Exception | None = None
    client: ChainClient | None = None
    w3: Web3 | None = None
    tx: dict | None = None

    for label, rpc_url in rpc_candidates:
        logger.info("Trying %s RPC: %s", label, rpc_url)
        client = ChainClient(rpc_urls=[rpc_url])
        w3 = client.w3
        try:
            tx = client.get_transaction(tx_hash)
            break
        except Exception as e:
            last_error = e
            if label == "mainnet" and _looks_like_tx_not_found(e):
                logger.warning(
                    "Tx not found on mainnet RPC; falling back to testnet (error: %s)", e
                )
                continue
            logger.error("get_transaction failed on %s: %s", label, e)
            print(f"\nError: Could not fetch transaction {tx_hash}")
            print(f"  {e}")
            sys.exit(1)

    if tx is None or client is None or w3 is None:
        assert last_error is not None
        logger.error("No RPC succeeded: %s", last_error)
        print(f"\nError: Could not fetch transaction {tx_hash}")
        print(f"  {last_error}")
        sys.exit(1)

    receipt = client.get_receipt(tx_hash)
    logger.debug("receipt present=%s", receipt is not None)

    # Fetch block metadata when the tx is mined
    block = None
    if receipt is not None:
        try:
            block = w3.eth.get_block(receipt.block_number)
        except Exception:
            pass

    _print_header(tx_hash, tx, receipt, block, w3)

    # Pending transaction — limited info available
    if receipt is None:
        print()
        print("Transaction is still in the mempool.")
        print(f"{'Nonce:':<20}{tx.get('nonce', '?')}")
        print(f"{'Gas Limit:':<20}{_fmt_number(tx.get('gas', 0))}")

        input_data = tx.get("input", b"")
        if input_data and input_data != b"" and input_data != "0x":
            decoded = TransactionDecoder.decode_function_call(input_data)
            _print_function(decoded, client, tx)
        print()
        return

    # Gas analysis
    if block is not None:
        _print_gas_analysis(tx, receipt, block)

    # Decoded function call
    input_data = tx.get("input", b"")
    if input_data and input_data != b"" and input_data != "0x":
        decoded = TransactionDecoder.decode_function_call(input_data)
        _print_function(decoded, client, tx)

    # Event logs (raw receipt needed for full log dicts)
    try:
        raw_receipt = w3.eth.get_transaction_receipt(tx_hash)
        raw_logs = list(raw_receipt["logs"])
    except Exception:
        raw_logs = []

    if raw_logs:
        events = TransactionDecoder.parse_events(raw_logs)
        logger.debug("parsed %s logs into events", len(raw_logs))
        _print_transfers(events, client)
        _print_swap_summary(tx, events, client)

    # Revert reason for failed transactions
    if not receipt.status:
        _print_revert_info(tx_hash, client)

    print("=" * 100)
    logger.info("analyze done: hash_prefix=%s", tx_hash[:12])


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry: validate hash, then :func:`analyze`."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Analyze an Ethereum transaction",
        prog="python -m chain.analyzer",
    )
    parser.add_argument("tx_hash", help="Transaction hash to analyze")
    parser.add_argument(
        "--rpc",
        default=_default_mainnet_rpc_url(),
        help=(
            "Primary JSON-RPC URL (default: $RPC_ENDPOINT, then a public mainnet). "
            "If the tx is not found there, the tool retries on $SEPOLIA_RPC."
        ),
    )

    args = parser.parse_args()

    log_path = configure_project_logging()
    logging.getLogger(__name__).info("log file: %s", log_path)

    try:
        tx_hash = normalize_tx_hash(args.tx_hash)
    except InvalidParameterError as err:
        print(f"\nInvalid transaction hash: {err}", file=sys.stderr)
        sys.exit(2)

    testnet_rpcs = _default_testnet_rpc_urls()
    analyze(tx_hash, args.rpc, testnet_rpcs)


if __name__ == "__main__":
    main()
