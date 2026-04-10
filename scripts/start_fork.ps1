#Requires -Version 5.1
<#
.SYNOPSIS
    Start a local Anvil fork of Ethereum mainnet.

.DESCRIPTION
    Requires Foundry (anvil). Resolves RPC URL from, in order:
    ETH_RPC_URL, MAINNET_RPC, RPC_ENDPOINT (same idea as docs/setup.md).

.EXAMPLE
    $env:ETH_RPC_URL = 'https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY'
    .\scripts\start_fork.ps1

    Then in another terminal:
    $env:FORK_RPC_URL = 'http://127.0.0.1:8545'
    pytest -m fork tests/test_fork_simulator_integration.py
#>

$ErrorActionPreference = "Stop"

# PowerShell does not read .env by itself; load repo-root .env into this process (no override).
$repoRoot = Split-Path -Parent $PSScriptRoot
$dotEnv = Join-Path $repoRoot ".env"
if (Test-Path -LiteralPath $dotEnv) {
    Get-Content -LiteralPath $dotEnv -Encoding utf8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) {
            return
        }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) {
            return
        }
        $key = $line.Substring(0, $eq).Trim()
        $val = $line.Substring($eq + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $existing = [Environment]::GetEnvironmentVariable($key, "Process")
        if (-not [string]::IsNullOrEmpty($existing)) {
            return
        }
        Set-Item -Path "env:$key" -Value $val
    }
}

$rpc = $env:ETH_RPC_URL
if (-not $rpc) { $rpc = $env:MAINNET_RPC }
if (-not $rpc) { $rpc = $env:RPC_ENDPOINT }

if (-not $rpc) {
    Write-Error "Set ETH_RPC_URL, MAINNET_RPC, or RPC_ENDPOINT to a mainnet JSON-RPC URL."
    exit 1
}

$anvil = Get-Command anvil -ErrorAction SilentlyContinue
if (-not $anvil) {
    Write-Error "anvil not found. Install Foundry: https://book.getfoundry.sh/getting-started/installation"
    exit 1
}

# Omit --fork-block-number: newer anvil requires a numeric block, not "latest";
# with only --fork-url, the fork uses the node's current tip.
& anvil `
    --fork-url $rpc `
    --port 8545 `
    --accounts 10 `
    --balance 10000
