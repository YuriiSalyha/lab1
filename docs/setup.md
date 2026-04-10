# Setup

## Requirements

- **Python 3.10+** (matches Ruff `target-version` in `pyproject.toml`)
- **Windows** for the scripted path below (`run.ps1`). On macOS/Linux, use the same commands inside a venv manually (see bottom).

## Quick start (Windows)

From the repo root:

```powershell
.\run.ps1 install
```

This creates `venv\`, installs the package in editable mode with **`[dev]`** extras from **`pyproject.toml`** (runtime deps plus **ruff**, **pytest**, **pre-commit**), and runs `pre-commit install`.

## Transaction analyzer (mainnet)

The analyzer expects an **Ethereum mainnet** JSON-RPC URL by default. Resolution order: **`MAINNET_RPC`**, **`ETH_MAINNET_RPC`**, **`RPC_ENDPOINT`**, then a public mainnet fallback. A single Alchemy mainnet URL in `RPC_ENDPOINT` is enough. If you point `RPC_ENDPOINT` at Sepolia for other scripts, add **`MAINNET_RPC`** with a mainnet URL so Etherscan transaction hashes still resolve.

## Environment variables

Optional. For local secrets, copy the example and edit:

```powershell
copy .env.example .env
```

Tests use `load_dotenv()`; the current suite does not require `.env` to pass.

## Everyday commands

| Task   | Command              |
|--------|----------------------|
| Lint   | `.\run.ps1 lint`     |
| Tests  | `.\run.ps1 test`     |
| Run app| `.\run.ps1 start`    |
| Analyze tx | `.\run.ps1 analyze <tx_hash> [--rpc URL]` |
| Integration tests | `.\run.ps1 integration` (runs `scripts/integration_test_week1.py`) |
| Price impact table (mainnet) | `.\run.ps1 pricing-impact -- --pool 0x... --token USDC` (pool ticker you sell); needs HTTP RPC env or `--rpc` |
| Best route | `.\run.ps1 pricing-route -- --token-in 0x... --token-out 0x... --amount HUMAN` — optional `--pools`, `--discover fetch` or `cache` (subgraph env); same HTTP RPC as above |
| Mempool Uniswap V2 swaps | `.\run.ps1 pricing-mempool` — needs `MAINNET_WS` / `WS_URL` / `ALCHEMY_WS` or `--ws` (`wss://...`) |

Direct Python (from repo root, venv on): `python scripts/pricing_impact_table.py --help`, etc.

## Integration test (Sepolia)

The Sepolia integration test sends a real ETH transfer on the Sepolia testnet.

### Prerequisites

1. A Sepolia wallet funded with at least **0.001 ETH** (use the [Sepolia faucet](https://sepoliafaucet.com/)).
2. A Sepolia RPC endpoint (e.g. [Alchemy](https://www.alchemy.com/) or [Infura](https://infura.io/)).

### Environment variables

| Variable | Required | Default |
|----------|----------|---------|
| `PRIVATE_KEY` | Yes | — |
| `SEPOLIA_RPC` | No | Falls back to `RPC_ENDPOINT`, then `https://rpc.sepolia.org` |
| `TEST_RECIPIENT` | No | `0x...dEaD` (burn address) |

### Running

```powershell
$env:PRIVATE_KEY="0xYourSepoliaKey"
$env:SEPOLIA_RPC="https://eth-sepolia.g.alchemy.com/v2/YOUR_KEY"
.\run.ps1 integration
```

Or directly:

```powershell
$env:PRIVATE_KEY="0x..."; python scripts/integration_test_week1.py
```

## Without `run.ps1`

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Unix:    source venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest tests/
ruff check . --fix
python src/main.py
```
