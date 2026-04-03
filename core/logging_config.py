"""Configure root logging: UTF-8 log file plus optional stderr."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from core.errors import WalletValidationError
from core.validation import validate_log_file_param

_DEFAULT_REL = Path("logs") / "lab1.log"


def configure_project_logging(
    *,
    log_file: str | Path | None = None,
    level: int = logging.INFO,
    log_to_console: bool = True,
    console_level: int = logging.ERROR,
) -> Path:
    """Attach a file handler and optionally stderr to the root logger.

    Parent directories for the log file are created as needed.

    ``LOG_FILE`` in the environment overrides the default path ``logs/lab1.log``
    (relative to the process working directory) when *log_file* is omitted.

    The log file receives records at *level* (default INFO) and above.
    Stderr only receives *console_level* and above (default ERROR).

    Returns:
        Resolved path to the log file.
    """
    validate_log_file_param(log_file)
    if not isinstance(level, int) or isinstance(level, bool):
        raise WalletValidationError("level must be an integer logging level.")
    if not isinstance(console_level, int) or isinstance(console_level, bool):
        raise WalletValidationError("console_level must be an integer logging level.")
    if not isinstance(log_to_console, bool):
        raise WalletValidationError("log_to_console must be a boolean.")

    path = Path(log_file or os.getenv("LOG_FILE", _DEFAULT_REL))
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(min(level, console_level))

    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    if log_to_console:
        console_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(console_level)
        sh.setFormatter(console_fmt)
        root.addHandler(sh)

    return path.resolve()
