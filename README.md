# lab1

Python toolkit for **Ethereum account handling**, **JSON-RPC access**, **transaction analysis**, and **Uniswap V2–style pricing** (routing, fork simulation, mempool decoding)—with deterministic JSON utilities, linting, and tests wired for local development.

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
- **`chain.uniswap_v2_router`** — Encodes/decodes Uniswap V2 router swap calldata and return data; metadata is shared with `TransactionDecoder` so selectors stay aligned.
- **CLI analyzer** — Inspect any mined or pending transaction from the command line:

  ```powershell
  python -m chain.analyzer 0x<64-hex-tx-hash> [--rpc https://...]
  ```

  Defaults to **Ethereum mainnet**: `MAINNET_RPC`, then `ETH_MAINNET_RPC`, then `RPC_ENDPOINT`, then a public mainnet RPC. If `RPC_ENDPOINT` is a testnet, set `MAINNET_RPC` to a mainnet URL or pass `--rpc`. Use a **full 32-byte transaction hash** (64 hex digits), not an address.

### `pricing/`

- **`UniswapV2Pair`** — Integer constant-product math (`get_amount_out` / `get_amount_in`), price impact helpers, optional **`from_chain`** via `ChainClient`.
- **`Route` / `RouteFinder`** — Multi-hop paths; **`find_best_route`** maximizes net output after gas priced in the output token.
- **`PriceImpactAnalyzer`** — Trade-size impact tables and cost estimates including gas.
- **`ParsedSwap` / `MempoolMonitor`** — Decode Uniswap V2 router swaps from pending transactions over WebSocket (`await monitor.start()`).
- **`ForkSimulator`** — Simulate router swaps on a local fork (`eth_call`); **`compare_simulation_vs_calculation`** checks AMM math vs fork for a single hop.
- **`PricingEngine`** — **`get_quote`** combines routing + fork verification; **`Quote`** / **`QuoteError`**; optional mempool callbacks for swaps touching loaded pools.

See **[docs/Week2.md](docs/Week2.md)** for a full package overview. Local fork helper: **`scripts/start_fork.ps1`** (requires [Foundry](https://book.getfoundry.sh/) `anvil`). Optional pytest fork tests: set **`FORK_RPC_URL`** (e.g. `http://127.0.0.1:8545`) and run `pytest -m fork`.

## Requirements

- **Python 3.10+**
- Dependencies are listed in **`pyproject.toml`** (including `web3`, `eth-account`, `eth-abi`, `eth-utils`, `python-dotenv`). Dev tools (**`pytest`**, **`ruff`**, **`pre-commit`**) are optional extras: **`[dev]`**.

## Setup and commands

First-time setup and macOS/Linux equivalents: **[docs/setup.md](docs/setup.md)**.

```powershell
.\run.ps1 install   # venv, editable install, ruff, pytest, pre-commit
.\run.ps1 test
.\run.ps1 lint
.\run.ps1 start     # placeholder entry (src/main.py)
.\run.ps1 analyze 0x<64-hex-tx-hash> [--rpc https://...]   # transaction analyzer (needs venv)
.\run.ps1 integration   # full Sepolia suite: smoke + edge cases (needs PRIVATE_KEY)
.\run.ps1 pricing-impact -- --pool 0x... --token WETH    # + RPC env or --rpc; token = ticker you sell
.\run.ps1 pricing-route -- --token-in 0x... --token-out 0x... --amount 10000   # optional --pools; --discover fetch|cache + subgraph env
.\run.ps1 pricing-mempool  # pending Uniswap V2 swaps (needs wss:// in MAINNET_WS / WS_URL)
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
| `chain/` | RPC client, builder, decoder, Uniswap V2 router codec, analyzer CLI |
| `pricing/` | AMM pairs, routing, price impact, mempool monitor, fork simulator, pricing engine |
| `tests/` | Pytest suite (`core`, `chain`, `pricing`; optional `-m fork` with `FORK_RPC_URL`) |
| `scripts/` | Sepolia integration; `start_fork.ps1`; `pricing_impact_table.py`, `pricing_best_route.py`, `pricing_mempool_monitor.py` (see `run.ps1`) |
| `docs/` | Setup, **[Week1.md](docs/Week1.md)** (`core`/`chain`), **[Week2.md](docs/Week2.md)** (`pricing`) |

## Tooling

- **[Ruff](https://docs.astral.sh/ruff/)** — Lint (and optional format); config in `pyproject.toml`.
- **[pytest](https://pytest.org/)** — Tests; `pythonpath` includes the repo root for `import core` / `import chain` / `import pricing`.
- **Pre-commit** — Hooks in `.pre-commit-config.yaml` (includes **`detect-private-key`**). Run after clone: `.\run.ps1 install` or `pre-commit install` inside the venv.

## Why `run.ps1`?

On Windows, **PowerShell** avoids relying on GNU Make and Unix-only paths (`venv/bin`, etc.). See **`docs/setup.md`** for bash-friendly commands if you are not using PowerShell.
