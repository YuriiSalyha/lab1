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
        & "$venv_bin\pip" install -e .
        & "$venv_bin\pip" install ruff pytest pre-commit
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
    default {
        Write-Host "Usage: .\run.ps1 [install|lint|test|start|analyze|integration]" -ForegroundColor Yellow
        Write-Host "  analyze      -> python -m chain.analyzer (pass tx hash and optional --rpc)" -ForegroundColor Gray
        Write-Host "  integration  -> Week 1 Sepolia suite (scripts/integration_test_week1.py)" -ForegroundColor Gray
    }
}
