"""Unit tests for core/recorder.py - resolution logic and state detection."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.recorder import (
    PSRNotFoundError,
    Recorder,
    _BUNDLED_LNK,
    _resolve_bundled_asset_dir,
)


# ------------------------------------------------------------------
# Helper: build a Recorder without actually searching for psr.exe
# ------------------------------------------------------------------

def _make_recorder(exe_path: Path) -> Recorder:
    """Return a Recorder whose _psr_exe is forced to exe_path (no disk lookup)."""
    r = object.__new__(Recorder)
    r._psr_exe = exe_path
    r._pending_output = None
    return r


# ------------------------------------------------------------------
# Resolution tiers
# ------------------------------------------------------------------

def test_resolve_uses_path_lookup_first(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    with patch("psr_ai.core.recorder.shutil.which", return_value=str(fake_psr)):
        r = Recorder()
    assert r._psr_exe == fake_psr


def test_resolve_falls_through_to_tier1(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    # PATH misses, but %SystemRoot%\System32\psr.exe exists.
    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=[fake_psr, Exception("not called")]),
    ):
        r = Recorder()
    assert r._psr_exe == fake_psr


def test_resolve_raises_when_all_tiers_fail():
    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("nope")),
        patch.object(Path, "exists", return_value=False),
    ):
        with pytest.raises(PSRNotFoundError):
            Recorder()


def test_bundled_lnk_path_exists():
    # The .lnk asset must be present in the repo at the expected location.
    assert _BUNDLED_LNK.exists(), (
        f"Bundled shortcut not found at {_BUNDLED_LNK}. "
        "Run: git lfs pull  or check assets/psr_fallback/."
    )


def test_resolve_falls_through_to_bundled_exe_when_system_psr_absent(tmp_path):
    """Tier 3a: when system PSR is missing, use bundled psr.exe if present."""
    fake_bundled_exe = tmp_path / "psr.exe"
    fake_bundled_exe.touch()

    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
        patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
    ):
        r = Recorder()

    assert r._psr_exe == fake_bundled_exe
    assert r._psr_exe.suffix == ".exe"
    assert r._use_shell is False


def test_resolve_falls_through_to_bundled_lnk_when_exe_absent(tmp_path):
    """Tier 3b: bundled .lnk used when no portable exe is present."""
    fake_lnk = tmp_path / "psr_shortcut.lnk"
    fake_lnk.touch()
    # No psr.exe in tmp_path - only the .lnk.

    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
        patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
    ):
        r = Recorder()

    assert r._psr_exe == fake_lnk
    assert r._use_shell is True


def test_error_message_contains_manual_steps_when_all_tiers_fail(tmp_path):
    """PSRNotFoundError must include actionable recovery instructions."""
    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
        patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
    ):
        # tmp_path has neither psr.exe nor psr_shortcut.lnk.
        with pytest.raises(PSRNotFoundError) as exc_info:
            Recorder()

    msg = str(exc_info.value)
    assert "Option A" in msg
    assert "Option B" in msg
    assert "Option C" in msg
    assert "gpedit.msc" in msg or "Group Policy" in msg


def test_resolve_bundled_asset_dir_returns_a_path():
    result = _resolve_bundled_asset_dir()
    assert isinstance(result, Path)


def test_bundled_exe_takes_priority_over_lnk(tmp_path):
    """When both psr.exe and psr_shortcut.lnk exist, the .exe wins."""
    fake_exe = tmp_path / "psr.exe"
    fake_lnk = tmp_path / "psr_shortcut.lnk"
    fake_exe.touch()
    fake_lnk.touch()

    with (
        patch("psr_ai.core.recorder.shutil.which", return_value=None),
        patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
        patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
    ):
        r = Recorder()

    assert r._psr_exe == fake_exe


# ------------------------------------------------------------------
# _use_shell property
# ------------------------------------------------------------------

def test_use_shell_false_for_exe(tmp_path):
    r = _make_recorder(tmp_path / "psr.exe")
    assert r._use_shell is False


def test_use_shell_true_for_lnk(tmp_path):
    r = _make_recorder(tmp_path / "psr_shortcut.lnk")
    assert r._use_shell is True


# ------------------------------------------------------------------
# is_recording_active
# ------------------------------------------------------------------

def test_is_recording_active_true_when_process_found(tmp_path):
    r = _make_recorder(tmp_path / "psr.exe")
    mock_result = MagicMock()
    mock_result.stdout = "psr.exe  1234  Console  1  10,000 K"
    with patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result):
        assert r.is_recording_active() is True


def test_is_recording_active_false_when_no_process(tmp_path):
    r = _make_recorder(tmp_path / "psr.exe")
    mock_result = MagicMock()
    mock_result.stdout = "INFO: No tasks are running which match the specified criteria."
    with patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result):
        assert r.is_recording_active() is False


def test_is_recording_active_false_when_tasklist_missing(tmp_path):
    r = _make_recorder(tmp_path / "psr.exe")
    with patch("psr_ai.core.recorder.subprocess.run", side_effect=FileNotFoundError):
        assert r.is_recording_active() is False


# ------------------------------------------------------------------
# start_recording
# ------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="PSR is Windows-only")
def test_start_recording_creates_output_parent(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    r = _make_recorder(fake_psr)

    output = tmp_path / "sessions" / "recording.zip"
    with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
        result = r.start_recording(output_path=output)

    assert result == output.resolve()
    assert output.parent.exists()
    mock_popen.assert_called_once()
    args_passed = mock_popen.call_args[0][0]
    assert "/start" in args_passed
    assert "/output" in args_passed


def test_start_recording_filename_has_no_colons(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    r = _make_recorder(fake_psr)

    with patch("psr_ai.core.recorder.subprocess.Popen"):
        result = r.start_recording()

    assert ":" not in result.name


# ------------------------------------------------------------------
# stop_recording
# ------------------------------------------------------------------

def test_stop_recording_returns_pending_output(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    r = _make_recorder(fake_psr)

    expected = tmp_path / "recording.zip"
    r._pending_output = expected

    # _launch uses Popen internally; mock it so no real process is spawned.
    with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
        result = r.stop_recording()

    assert result == expected
    mock_popen.assert_called_once()
    args_passed = mock_popen.call_args[0][0]
    assert "/stop" in args_passed
