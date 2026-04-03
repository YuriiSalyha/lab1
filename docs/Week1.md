# Week 1 — `core` and `chain` packages

This document summarizes what the **`core`** and **`chain`** packages provide, how they fit together, and what was implemented (architecture, CLI, error handling, and documentation/logging passes).

---

## Big picture

| Layer | Role |
|--------|------|
| **`core`** | Domain types (addresses, amounts, tx requests/receipts), wallet signing, deterministic JSON for hashing, shared exceptions. No Ethereum RPC by itself. |
| **`chain`** | JSON-RPC client (Web3), transaction building/sending, calldata & event decoding, a CLI analyzer, nonce management, chain-specific errors. |

`chain` depends on `core` (e.g. `Address`, `TokenAmount`, `TransactionRequest`, `WalletManager`).

---

## `core/` package

### `core.types`

- **`Address`** — Validates and EIP-55-checksums a `0x` address string; equality is case-insensitive.
- **`TokenAmount`** — Integer “atomic” amount + decimals; rejects float math; supports `from_human` via `Decimal`.
- **`Token`** — ERC-20 identity keyed by **`Address`** (symbol/decimals are metadata).
- **`TransactionRequest`** — Unsigned tx: `to`, `value`, `data`, optional gas/EIP-1559 fields, `chain_id`, optional `from_address`.
  **`from_dict` / `to_dict`** round-trip Web3-style keys (`maxFeePerGas`, `gas`, etc.).
  Helpers **`_parse_to_address`** and **`_parse_value_field`** keep that mapping in one place.
- **`TransactionReceipt`** — Normalized receipt; **`from_web3`** uses **`_receipt_tx_hash_hex`** so `transactionHash` works whether the node returns `HexBytes` or a string.

### `core.wallet` — `WalletManager`

Loads a private key (constructor, **`from_env`**, **`from_keyfile`**, or **`generate`**), then signs:

- arbitrary text (**EIP-191** via `sign_message`),
- **EIP-712** typed data (`sign_typed_data`),
- transactions (`sign_transaction`).

Errors from signing/encoding are passed through **`_validation_error_from_exception`** so messages are **sanitized** (private key substrings never leak). Logging uses **address suffixes** and **dict keys**, never secrets.

### `core.serializer` — `CanonicalSerializer`

Deterministic JSON (sorted keys, no floats, no sets) for signing/hashing. **`hash`** applies Keccak-256 to the canonical bytes.

### `core.errors`

Hierarchy: **`WalletError`** (optional `code` / `details`), **`WalletValidationError`**, **`InvalidAddressError`**, **`TokenMathError`**, **`WalletSecurityError`**.

### `core._secret_str` — `SecretStr`

Wraps secrets so `str` / `repr` / pickling do not expose raw values.

---

## `chain/` package

### `chain.client` — `ChainClient`

- Multiple **HTTP RPC URLs** → multiple `Web3` instances; **`_execute_with_retry`** rotates endpoints and backs off.
- **Typed errors** from RPC strings (`InsufficientFunds`, `NonceTooLow`, etc.) via **`_classify_error`**.
- **`TokenMetadataCache`** — lazy ERC-20 `symbol` / `decimals` / `name` per contract address.
- Wrappers: balance, nonce, gas estimate, gas price snapshot (**`GasPrice`**), `send_raw_transaction`, `wait_for_receipt`, `get_transaction`, `get_receipt`, `call`, **`get_revert_reason`**, **`check_node_health`**, **`decode_transaction`**, **`parse_receipt_events`**, **`get_tx_status`**.
- Receipts are normalized through a shared **`_receipt_from_web3`** helper.

### `chain.nonce_manager` — `NonceManager`

Per-address lock, syncs with **`pending`** transaction count, increments a local counter so concurrent sends do not reuse the same nonce.

### `chain.builder` — `TransactionBuilder`

Fluent API: `to`, `value`, `data`, optional `nonce`, `gas_limit`, **`with_gas_estimate`**, **`with_gas_price`**, then **`build`**, **`build_and_sign`**, **`send`**, **`send_and_wait`**. Nonce is auto-filled if omitted. **`_validate_balance`** checks ETH for value + max gas cost before signing.

### `chain.decoder` — `TransactionDecoder`

- **`decode_function_call`** — Known selectors (ERC-20, Uniswap V2 router, partial V3 names) with ABI-style **`signature`** and ordered **`param_names`**; unknown calldata still returns selector + raw hex.
- **`parse_event` / `parse_events`** — `Transfer`, `Approval`, Uniswap V2 **`Swap`**, V3 **`SwapV3`**, **`Sync`**, plus unknown logs.
- **`decode_revert_reason`** — `Error(string)` / `Panic` payloads.

Shared helpers reduce duplication: **`_function_decode_result`**, **`_calldata_to_bytes`**, **`_log_data_bytes`**, **`_address_from_topic`**, **`_try_decode_uint256`**.

### `chain.helpers`

Small functions used by the analyzer: **`token_symbol_and_decimals`**, **`format_human_token_amount`** (keeps `client` imports acyclic).

### `chain.analyzer` — CLI

```bash
python -m chain.analyzer <64-hex-tx-hash> [--rpc URL]
```

- Validates full **32-byte** tx hashes (rejects 20-byte “address-length” inputs that Web3 would pad incorrectly).
- Prints transaction summary, gas breakdown, decoded function (with human-readable args where metadata exists), token transfers, swap-style summary from transfers, and revert info when status failed.
- **`logging.basicConfig(INFO)`** in **`main()`**; logs hash **prefixes** and counts, not full RPC payloads.

### `chain.errors`

Exceptions for RPC, timeouts, node lag, gas estimation, replacement policy, etc. **`TransactionFailed`** can carry a **`revert_reason`**.

---

## Cross-cutting work (both packages)

1. **DRY** — Shared helpers for repeated patterns (see `core.types` parsing, `wallet` error sanitization, `decoder` result shapes, `client` receipt wrapping, `helpers` for token display).
2. **Logging** — `logging.getLogger(__name__)`; **INFO** for lifecycle (client init, submit, receipt, keyfile), **DEBUG** for fine-grained non-sensitive detail, **WARNING/ERROR** for failures; never log private keys or full transaction bodies.
3. **Docstrings** — Modules, public classes, and public methods document purpose, arguments, and return values where it helps maintenance.

---

## Related docs

- **[setup.md](setup.md)** — environment, `run.ps1`, lint/tests.
