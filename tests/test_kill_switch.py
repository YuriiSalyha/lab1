"""Kill switch file presence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from risk.kill_switch import default_kill_switch_path, is_kill_switch_active


def test_kill_switch_inactive_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "not_there"
    assert is_kill_switch_active(p) is False


def test_kill_switch_active_when_file_exists(tmp_path: Path) -> None:
    p = tmp_path / "kill"
    p.write_text("", encoding="utf-8")
    assert is_kill_switch_active(p) is True


def test_kill_switch_custom_path_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "ks"
    monkeypatch.setenv("ARB_KILL_SWITCH_FILE", str(p))
    assert is_kill_switch_active() is False
    p.write_text("x", encoding="utf-8")
    assert is_kill_switch_active() is True


def test_default_kill_switch_path_uses_tmp_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_KILL_SWITCH_FILE", raising=False)
    monkeypatch.setattr("risk.kill_switch.platform.system", lambda: "Linux")
    assert default_kill_switch_path() == Path("/tmp/arb_bot_kill")


def test_default_kill_switch_path_uses_tempdir_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_KILL_SWITCH_FILE", raising=False)
    monkeypatch.setattr("risk.kill_switch.platform.system", lambda: "Windows")
    assert default_kill_switch_path() == Path(tempfile.gettempdir()) / "arb_bot_kill"
