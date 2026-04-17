# Demo entrypoints for lab1 (Binance testnet, mainnet/sepolia RPC, .env secrets).
# Run from repo root: .\scripts\show_lab.ps1 <demo> [extra args for arb only]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("testnet-ioc", "portfolio", "arb", "pnl")]
    [string] $Demo,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$Py = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "venv not found. Run: .\run.ps1 install" -ForegroundColor Yellow
    exit 1
}

switch ($Demo) {
    "testnet-ioc" {
        $pytest = Join-Path $RepoRoot "venv\Scripts\pytest.exe"
        & $pytest `
            "tests/test_exchange_client.py::test_integration_limit_ioc_place_and_cancel" `
            -v -m integration @RemainingArgs
    }
    "portfolio" {
        & $Py "scripts/portfolio_snapshot.py" @RemainingArgs
    }
    "arb" {
        if (-not $RemainingArgs -or $RemainingArgs.Count -eq 0) {
            Write-Host "Usage: .\scripts\show_lab.ps1 arb ETH/USDT --size 1.0 [--rpc URL] [--pool 0x...] [--gas-usd 5]" -ForegroundColor Yellow
            Write-Host "  Env: MAINNET_RPC, ARB_V2_POOL (or pass flags)" -ForegroundColor Gray
            exit 1
        }
        & $Py "scripts/arb_checker.py" @RemainingArgs
    }
    "pnl" {
        & $Py "scripts/pnl_demo.py" @RemainingArgs
    }
}
