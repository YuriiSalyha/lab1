"""Process-local kill switch via sentinel file."""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path

_ENV_KILL_PATH = "ARB_KILL_SWITCH_FILE"
_DEFAULT_KILL_NAME = "arb_bot_kill"


def default_kill_switch_path() -> Path:
    """Default sentinel path.

    - If ``ARB_KILL_SWITCH_FILE`` is set, use that path (instructor / ops standard).
    - On non-Windows hosts, default to ``/tmp/arb_bot_kill`` (matches typical lab VM).
    - On Windows, use the process temp directory (``%TEMP%`` / ``tempfile.gettempdir()``).
    """
    custom = os.getenv(_ENV_KILL_PATH, "").strip()
    if custom:
        return Path(custom)
    if platform.system() != "Windows":
        return Path("/tmp") / _DEFAULT_KILL_NAME
    return Path(tempfile.gettempdir()) / _DEFAULT_KILL_NAME


def is_kill_switch_active(path: Path | None = None) -> bool:
    p = path if path is not None else default_kill_switch_path()
    try:
        return p.exists()
    except OSError:
        return False
