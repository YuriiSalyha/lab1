# lab1

Small Python project template with linting, tests, and pre-commit wired for local and CI use.

## What’s in place

- **Project template (Python)** — `pyproject.toml`, `src/`, `tests/`, shared config for Ruff and pytest.
- **Linter / formatter and test runner** — [Ruff](https://docs.astral.sh/ruff/) (lint + format) and [pytest](https://pytest.org/), configured in `pyproject.toml`.
- **Pre-commit** — hooks in `.pre-commit-config.yaml` (Ruff, YAML checks, whitespace, **`detect-private-key`**). Run once after clone: `.\run.ps1 install` (or `pre-commit install` inside the venv).
- **`.env.example` and keeping secrets out of git** — real values live in `.env` (listed in `.gitignore`); only the example file is committed so collaborators know which variables exist.
- **Two placeholder tests** — `tests/test_logic.py` exercises env loading and a trivial invariant so **pytest always has something to run** in CI and locally.

## Setup and commands

See **[docs/setup.md](docs/setup.md)** for Python version, first-time install, and equivalent commands without PowerShell.

```powershell
.\run.ps1 install   # venv + deps + pre-commit hooks
.\run.ps1 test
.\run.ps1 lint
.\run.ps1 start
```

## Why `run.ps1` instead of a Makefile?

On Windows, a **Makefile** usually assumes **GNU Make** and a Unix-style shell (paths like `venv/bin/activate`, `export`, often `bash`). That means extra installs (MSYS2, Chocolatey `make`, WSL) and fragile recipes when teammates are on stock Windows + PowerShell.

**PowerShell (`run.ps1`)** fits the default Windows toolchain: no separate `make` binary, native path handling for `venv\Scripts\`, and the same script works with `python` from PATH. Cross-platform or Linux/macOS users can still follow the shell equivalents in `docs/setup.md`. If the repo later standardizes on WSL or CI-only Linux, adding a thin `Makefile` that delegates to the same commands is optional; for a Windows-first dev loop, `.ps1` is the lower-friction default.
