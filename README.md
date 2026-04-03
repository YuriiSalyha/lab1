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

  Defaults to **Ethereum mainnet**: `MAINNET_RPC`, then `ETH_MAINNET_RPC`, then `RPC_ENDPOINT`, then a public mainnet RPC. If `RPC_ENDPOINT` is a testnet, set `MAINNET_RPC` to a mainnet URL or pass `--rpc`. Use a **full 32-byte transaction hash** (64 hex digits), not an address.

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
.\run.ps1 integration   # full Sepolia suite: smoke + edge cases (needs PRIVATE_KEY)
```

Copy **`.env.example`** → **`.env`** when you need RPC URLs or secrets locally (see setup doc).

### Integration tests (Sepolia)

``.\run.ps1 integration`` runs ``scripts/integration_test_week1.py`` (smoke transfer, then pytest edge cases). One-liner: ``python scripts/integration_test_week1.py``.

**Smoke step:** sends a small ETH transfer on Sepolia, verifies the signature locally, waits for confirmation, and checks the receipt.

```powershell
$env:PRIVATE_KEY="0x..."
$env:SEPOLIA_RPC="https://eth-sepolia.g.alchemy.com/v2/YOUR_KEY"
.\run.ps1 integration
```

Expected output:

```
Wallet: 0xYourAddress
Balance: 0.5 ETH

Building transaction...
  To: 0xTestRecipient
  Value: 0.0001 ETH
  Estimated Gas: 21000
  Max Fee: 35 gwei
  Max Priority: 2 gwei

Signing...
  Signature valid: ✓
  Recovered address matches: ✓

Sending...
  TX Hash: 0x...

Waiting for confirmation...
  Block: 1234567
  Status: SUCCESS
  Gas Used: 21000 (100%)
  Fee: 0.000735 ETH

Integration test PASSED
```

Requires a funded Sepolia wallet (faucet: https://sepoliafaucet.com/). See **[docs/setup.md](docs/setup.md)** for details.

## Project layout

| Path | Purpose |
|------|---------|
| `core/` | Domain types, wallet, serializer, errors |
| `chain/` | RPC client, builder, decoder, analyzer CLI |
| `tests/` | Pytest suite (unit tests for `core` and `chain`) |
| `scripts/` | `integration_test_week1.py` — Sepolia smoke + edge-case tests |
| `docs/` | Setup guide and additional notes (e.g. package overview in `docs/Week1.md`) |

## Tooling

- **[Ruff](https://docs.astral.sh/ruff/)** — Lint (and optional format); config in `pyproject.toml`.
- **[pytest](https://pytest.org/)** — Tests; `pythonpath` includes the repo root for `import core` / `import chain`.
- **Pre-commit** — Hooks in `.pre-commit-config.yaml` (includes **`detect-private-key`**). Run after clone: `.\run.ps1 install` or `pre-commit install` inside the venv.

## Why `run.ps1`?

On Windows, **PowerShell** avoids relying on GNU Make and Unix-only paths (`venv/bin`, etc.). See **`docs/setup.md`** for bash-friendly commands if you are not using PowerShell.
