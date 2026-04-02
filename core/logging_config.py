"""Configure root logging: UTF-8 log file plus optional stderr."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_DEFAULT_REL = Path("logs") / "lab1.log"


def configure_project_logging(
    *,
    log_file: str | Path | None = None,
    level: int = logging.INFO,
    log_to_console: bool = True,
) -> Path:
    """Attach a file handler and optionally stderr to the root logger.

    Parent directories for the log file are created as needed.

    ``LOG_FILE`` in the environment overrides the default path ``logs/lab1.log``
    (relative to the process working directory) when *log_file* is omitted.

    Returns:
        Resolved path to the log file.
    """
    path = Path(log_file or os.getenv("LOG_FILE", _DEFAULT_REL))
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    if log_to_console:
        console_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(console_fmt)
        root.addHandler(sh)

    return path.resolve()
