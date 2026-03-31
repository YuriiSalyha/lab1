# run.ps1
param($task)

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
    default { Write-Host "Usage: .\run.ps1 [install|lint|test|start]" }
}
