# run.ps1
param(
    [Parameter(Position = 0)]
    [string] $task,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

$python = "python"
$venv_bin = "venv\Scripts"

switch ($task) {
    "install" {
        & $python -m venv venv
        & "$venv_bin\pip" install -e ".[dev]"
        & "$venv_bin\pre-commit" install
        Write-Host "Setup Completed" -ForegroundColor Green
    }
    "lint" { & "$venv_bin\ruff" check . --fix }
    "test" { & "$venv_bin\pytest" tests/ }
    "start" { & "$venv_bin\python" src/main.py }
    "analyze" {
        if (-not $RemainingArgs -or $RemainingArgs.Count -eq 0) {
            Write-Host "Usage: .\run.ps1 analyze <tx_hash> [--rpc URL]" -ForegroundColor Yellow
            exit 1
        }
        & "$venv_bin\python" -m chain.analyzer @RemainingArgs
    }
    "integration" {
        & "$venv_bin\python" scripts/integration_test_week1.py @RemainingArgs
    }
    "pricing-impact" {
        & "$venv_bin\python" scripts/pricing_impact_table.py @RemainingArgs
    }
    "pricing-route" {
        & "$venv_bin\python" scripts/pricing_best_route.py @RemainingArgs
    }
    "pricing-mempool" {
        & "$venv_bin\python" scripts/pricing_mempool_monitor.py @RemainingArgs
    }
    "pricing-arb" {
        & "$venv_bin\python" scripts/pricing_arbitrage_scan.py @RemainingArgs
    }
    "pricing-ws-feed" {
        & "$venv_bin\python" scripts/pricing_ws_price_feed.py @RemainingArgs
    }
    "pricing-history-impact" {
        & "$venv_bin\python" scripts/pricing_historical_impact.py @RemainingArgs
    }
    "orderbook" {
        if (-not $RemainingArgs -or $RemainingArgs.Count -eq 0) {
            Write-Host "Usage: .\run.ps1 orderbook <SYMBOL> [--depth N] [--depth-bps N] [--walk 2,10]" -ForegroundColor Yellow
            Write-Host "  Example: .\run.ps1 orderbook ETH/USDT --depth 20" -ForegroundColor Gray
            exit 1
        }
        & "$venv_bin\python" -m exchange.orderbook @RemainingArgs
    }
    "demo" {
        & powershell -NoProfile -File "$PSScriptRoot\scripts\show_lab.ps1" @RemainingArgs
    }
    "demo-portfolio" { & "$venv_bin\python" scripts/portfolio_snapshot.py @RemainingArgs }
    "demo-pnl" { & "$venv_bin\python" scripts/pnl_demo.py @RemainingArgs }
    "demo-arb" { & "$venv_bin\python" scripts/arb_checker.py @RemainingArgs }
    "demo-testnet-order" {
        & "$venv_bin\pytest" tests/test_exchange_client.py::test_integration_limit_ioc_place_and_cancel `
            -v -m integration @RemainingArgs
    }
    default {
        Write-Host "Usage: .\run.ps1 [install|lint|test|start|analyze|integration|pricing-impact|pricing-route|pricing-mempool|pricing-arb|pricing-ws-feed|pricing-history-impact|orderbook|demo|demo-portfolio|demo-pnl|demo-arb|demo-testnet-order]" -ForegroundColor Yellow
        Write-Host "  analyze          -> python -m chain.analyzer (pass tx hash and optional --rpc)" -ForegroundColor Gray
        Write-Host "  integration      -> Week 1 Sepolia suite (scripts/integration_test_week1.py)" -ForegroundColor Gray
        Write-Host "  pricing-impact   -> price impact (needs -- --pool 0x... --token SYMBOL)" -ForegroundColor Gray
        Write-Host "  pricing-route    -> best route (--token-in/out/amount; optional --discover fetch|cache)" -ForegroundColor Gray
        Write-Host "  pricing-mempool  -> pending Uniswap V2 swaps via WS (scripts/pricing_mempool_monitor.py)" -ForegroundColor Gray
        Write-Host "  pricing-arb      -> cyclic arb scan on V2 pools (scripts/pricing_arbitrage_scan.py)" -ForegroundColor Gray
        Write-Host "  pricing-ws-feed  -> V2 pair Sync stream over WebSocket (reserves / spot / optional impact)" -ForegroundColor Gray
        Write-Host "  pricing-history-impact -> historical impact from Sync logs (HTTP archive RPC)" -ForegroundColor Gray
        Write-Host "  orderbook        -> Binance testnet order book analysis (python -m exchange.orderbook)" -ForegroundColor Gray
        Write-Host "  demo             -> scripts/show_lab.ps1 (testnet-ioc|portfolio|arb|pnl)" -ForegroundColor Gray
        Write-Host "  demo-testnet-order -> pytest IOC + cancel integration (needs testnet API keys)" -ForegroundColor Gray
        Write-Host "  demo-portfolio   -> JSON snapshot Binance + wallet ETH (scripts/portfolio_snapshot.py)" -ForegroundColor Gray
        Write-Host "  demo-arb         -> arb_checker.py (pass pair, --size, optional --rpc --pool)" -ForegroundColor Gray
        Write-Host "  demo-pnl         -> five synthetic trades PnL summary (scripts/pnl_demo.py)" -ForegroundColor Gray
    }
}
