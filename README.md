# lab1

Python toolkit for **Ethereum account handling**, **JSON-RPC access**, and **transaction analysis**—with deterministic JSON utilities, linting, and tests wired for local development.

## Features

### `core/`

- **Types** — `Address`, `TokenAmount`, `Token`, `TransactionRequest` (Web3 dict round-trip), `TransactionReceipt`.
- **Wallet** — Load keys from env or encrypted keystore; sign messages (EIP-191), typed data (EIP-712), and transactions; secrets never leak into logs or errors.
- **Serializer** — Canonical JSON + Keccak hash for deterministic signing workflows.
- **Errors** — Typed exceptions for validation, security, and token math.

### `chain/`

- **ChainClient** — Multi-endpoint HTTP RPC with retries, EIP-1559 gas helpers, nonce management, receipts, `eth_call`, revert replay, and optional node health checks.
- **TransactionBuilder** — Fluent builder for gas estimation, fees, sign, broadcast, and wait-for-receipt.
- **TransactionDecoder** — Decodes common ERC-20 and Uniswap-style calldata; parses `Transfer`, `Swap`, `Sync`, and similar logs; extracts revert reasons when possible.
- **CLI analyzer** — Inspect any mined or pending transaction from the command line:

  ```powershell
  python -m chain.analyzer 0x<64-hex-tx-hash> [--rpc https://...]
  ```

  Set `RPC_ENDPOINT` or pass `--rpc`. Use a **full 32-byte transaction hash** (64 hex digits), not an address.

## Requirements

- **Python 3.10+**
- Dependencies are listed in **`pyproject.toml`** (including `web3`, `eth-account`, `eth-abi`, `python-dotenv`).

## Setup and commands

First-time setup and macOS/Linux equivalents: **[docs/setup.md](docs/setup.md)**.

```powershell
.\run.ps1 install   # venv, editable install, ruff, pytest, pre-commit
.\run.ps1 test
.\run.ps1 lint
.\run.ps1 start     # placeholder entry (src/main.py)
.\run.ps1 analyze 0x<64-hex-tx-hash> [--rpc https://...]   # transaction analyzer (needs venv)
```

Copy **`.env.example`** → **`.env`** when you need RPC URLs or secrets locally (see setup doc).

## Project layout

| Path | Purpose |
|------|---------|
| `core/` | Domain types, wallet, serializer, errors |
| `chain/` | RPC client, builder, decoder, analyzer CLI |
| `tests/` | Pytest suite (`core` coverage; run `pytest` from repo root) |
| `docs/` | Setup guide and additional notes (e.g. package overview in `docs/Week1.md`) |

## Tooling

- **[Ruff](https://docs.astral.sh/ruff/)** — Lint (and optional format); config in `pyproject.toml`.
- **[pytest](https://pytest.org/)** — Tests; `pythonpath` includes the repo root for `import core` / `import chain`.
- **Pre-commit** — Hooks in `.pre-commit-config.yaml` (includes **`detect-private-key`**). Run after clone: `.\run.ps1 install` or `pre-commit install` inside the venv.

## Why `run.ps1`?

On Windows, **PowerShell** avoids relying on GNU Make and Unix-only paths (`venv/bin`, etc.). See **`docs/setup.md`** for bash-friendly commands if you are not using PowerShell.
