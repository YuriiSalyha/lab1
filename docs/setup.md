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
| V2 live price feed (Sync logs) | `.\run.ps1 pricing-ws-feed -- --pool 0x...` — HTTP RPC for metadata + same WebSocket env as mempool |
| Historical V2 price impact | `.\run.ps1 pricing-history-impact -- --pool 0x... --from-block N --to-block M --token WETH --sizes 1e18` — **archive** HTTP RPC for old blocks |
| Binance testnet order book | `.\run.ps1 orderbook ETH/USDT --depth 20` — needs Binance testnet API keys in `.env` |
| Arb check (DEX + CEX + inventory) | `python -m scripts.arb_checker ETH/USDT --size 2.0 --rpc https://... --pool 0x...` — see **[Week3.md](Week3.md)** |

Direct Python (from repo root, venv on): `python scripts/pricing_impact_table.py --help`, etc.

### Package overviews

| Doc | Contents |
|-----|----------|
| **[Week1.md](Week1.md)** | `core`, `chain` |
| **[Week2.md](Week2.md)** | `pricing` |
| **[Week3.md](Week3.md)** | `exchange`, `inventory`, `scripts.arb_checker` |

### WebSocket vs HTTP for pricing scripts

- **Real-time pool reserves:** `pricing-ws-feed` subscribes to `Sync` logs over `wss://` (same URL variables as `pricing-mempool`). You still need an HTTP RPC to load pair metadata once at startup.
- **Historical impact:** `eth_getLogs` over HTTP; use **archive** RPC for old `from_block`. Alchemy **free** caps each log query to about **10 blocks**; the tool defaults `--chunk-blocks 10` and, if the node returns HTTP 400, **shrinks the span automatically** so a large `--chunk-blocks` still works (with extra round-trips).

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
