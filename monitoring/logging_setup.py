"""Arb bot logging: project file + optional daily ``logs/bot_YYYYMMDD.log``."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from core.logging_config import configure_project_logging

_ENV_DAILY_LOG = "ARB_BOT_DAILY_LOG"
_LOG_DIR = Path("logs")
_DAILY_PREFIX = "bot_"


def configure_arb_bot_logging() -> tuple[Path, Path | None]:
    """Primary log file (``LOG_FILE``) at INFO + console at INFO; optional daily shard."""
    primary = configure_project_logging(
        level=logging.INFO,
        console_level=logging.INFO,
        log_to_console=True,
    )
    daily_path: Path | None = None
    if os.getenv(_ENV_DAILY_LOG, "").strip().lower() in ("1", "true", "yes"):
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        daily_path = _LOG_DIR / f"{_DAILY_PREFIX}{day}.log"
        pipe_fmt = logging.Formatter(
            "%(asctime)s|%(levelname)s|%(name)s|%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        dh = logging.FileHandler(daily_path, encoding="utf-8")
        dh.setLevel(logging.INFO)
        dh.setFormatter(pipe_fmt)
        logging.getLogger().addHandler(dh)
    return primary, daily_path
