# Setup

## Requirements

- **Python 3.10+** (matches Ruff `target-version` in `pyproject.toml`)
- **Windows** for the scripted path below (`run.ps1`). On macOS/Linux, use the same commands inside a venv manually (see bottom).

## Quick start (Windows)

From the repo root:

```powershell
.\run.ps1 install
```

This creates `venv\`, installs **ruff**, **pytest**, **pre-commit**, and **python-dotenv**, and runs `pre-commit install`.

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

## Without `run.ps1`

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Unix:    source venv/bin/activate
pip install ruff pytest pre-commit python-dotenv
pre-commit install
pytest tests/
ruff check . --fix
python src/main.py
```
