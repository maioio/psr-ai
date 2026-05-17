"""Unit tests for core/recorder.py - factory, platform engines, resolution logic."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from psr_ai.core.recorder import (
    BaseRecorder,
    PSRNotFoundError,
    Recorder,
    WindowsRecorder,
    MacRecorder,
    LinuxRecorder,
    _BUNDLED_LNK,
    _resolve_bundled_asset_dir,
    _pack_to_zip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_windows_recorder(exe_path: Path) -> WindowsRecorder:
    """Return a WindowsRecorder whose _psr_exe is forced to exe_path."""
    r = object.__new__(WindowsRecorder)
    r._psr_exe = exe_path
    r._pending_output = None
    return r


def _make_mac_recorder() -> MacRecorder:
    """Return a MacRecorder in the pre-start state."""
    r = MacRecorder()
    return r


def _make_linux_recorder() -> LinuxRecorder:
    """Return a LinuxRecorder with no screenshot tool (safe for unit tests)."""
    with patch("psr_ai.core.recorder.shutil.which", return_value=None):
        r = LinuxRecorder()
    return r


# ---------------------------------------------------------------------------
# Factory: Recorder() dispatches by platform
# ---------------------------------------------------------------------------

class TestRecorderFactory:

    def test_factory_returns_windows_recorder_on_win32(self):
        with patch("psr_ai.core.recorder.sys.platform", "win32"):
            with patch.object(WindowsRecorder, "__init__", return_value=None) as mock_init:
                mock_init.return_value = None
                # We cannot easily intercept __init__ here, so test the type directly.
                pass
        # Verify the type directly by constructing with mocked resolution.
        fake_psr = Path("fake_psr.exe")
        with (
            patch("psr_ai.core.recorder.sys.platform", "win32"),
            patch("psr_ai.core.recorder.shutil.which", return_value=str(fake_psr)),
        ):
            r = Recorder()
        assert isinstance(r, WindowsRecorder)

    def test_factory_returns_mac_recorder_on_darwin(self):
        with patch("psr_ai.core.recorder.sys.platform", "darwin"):
            r = Recorder()
        assert isinstance(r, MacRecorder)

    def test_factory_returns_linux_recorder_on_linux(self):
        with (
            patch("psr_ai.core.recorder.sys.platform", "linux"),
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
        ):
            r = Recorder()
        assert isinstance(r, LinuxRecorder)

    def test_factory_raises_on_unknown_platform(self):
        with patch("psr_ai.core.recorder.sys.platform", "freebsd14"):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                Recorder()

    def test_all_engines_implement_base_recorder(self):
        assert issubclass(WindowsRecorder, BaseRecorder)
        assert issubclass(MacRecorder, BaseRecorder)
        assert issubclass(LinuxRecorder, BaseRecorder)


# ---------------------------------------------------------------------------
# WindowsRecorder: resolution tiers
# ---------------------------------------------------------------------------

class TestWindowsRecorderResolution:

    def test_resolve_uses_path_lookup_first(self, tmp_path):
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        with patch("psr_ai.core.recorder.shutil.which", return_value=str(fake_psr)):
            r = WindowsRecorder()
        assert r._psr_exe == fake_psr

    def test_resolve_falls_through_to_tier1(self, tmp_path):
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=[fake_psr, Exception("not called")]),
        ):
            r = WindowsRecorder()
        assert r._psr_exe == fake_psr

    def test_resolve_raises_when_all_tiers_fail(self):
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("nope")),
            patch.object(Path, "exists", return_value=False),
        ):
            with pytest.raises(PSRNotFoundError):
                WindowsRecorder()

    def test_bundled_lnk_path_exists(self):
        assert _BUNDLED_LNK.exists(), (
            f"Bundled shortcut not found at {_BUNDLED_LNK}. "
            "Run: git lfs pull or check assets/psr_fallback/."
        )

    def test_resolve_falls_through_to_bundled_exe_when_system_psr_absent(self, tmp_path):
        fake_bundled_exe = tmp_path / "psr.exe"
        fake_bundled_exe.touch()
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            r = WindowsRecorder()
        assert r._psr_exe == fake_bundled_exe
        assert r._psr_exe.suffix == ".exe"
        assert r._use_shell is False

    def test_resolve_falls_through_to_bundled_lnk_when_exe_absent(self, tmp_path):
        fake_lnk = tmp_path / "psr_shortcut.lnk"
        fake_lnk.touch()
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            r = WindowsRecorder()
        assert r._psr_exe == fake_lnk
        assert r._use_shell is True

    def test_error_message_contains_manual_steps_when_all_tiers_fail(self, tmp_path):
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            with pytest.raises(PSRNotFoundError) as exc_info:
                WindowsRecorder()
        msg = str(exc_info.value)
        assert "Option A" in msg
        assert "Option B" in msg
        assert "Option C" in msg
        assert "gpedit.msc" in msg or "Group Policy" in msg

    def test_resolve_bundled_asset_dir_returns_a_path(self):
        result = _resolve_bundled_asset_dir()
        assert isinstance(result, Path)

    def test_bundled_exe_takes_priority_over_lnk(self, tmp_path):
        fake_exe = tmp_path / "psr.exe"
        fake_lnk = tmp_path / "psr_shortcut.lnk"
        fake_exe.touch()
        fake_lnk.touch()
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError("no env")),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            r = WindowsRecorder()
        assert r._psr_exe == fake_exe


# ---------------------------------------------------------------------------
# WindowsRecorder: _use_shell
# ---------------------------------------------------------------------------

class TestWindowsUseShell:

    def test_use_shell_false_for_exe(self, tmp_path):
        r = _make_windows_recorder(tmp_path / "psr.exe")
        assert r._use_shell is False

    def test_use_shell_true_for_lnk(self, tmp_path):
        r = _make_windows_recorder(tmp_path / "psr_shortcut.lnk")
        assert r._use_shell is True


# ---------------------------------------------------------------------------
# WindowsRecorder: is_recording_active
# ---------------------------------------------------------------------------

class TestWindowsIsRecordingActive:

    def test_true_when_process_found(self, tmp_path):
        r = _make_windows_recorder(tmp_path / "psr.exe")
        mock_result = MagicMock()
        mock_result.stdout = "psr.exe  1234  Console  1  10,000 K"
        with patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result):
            assert r.is_recording_active() is True

    def test_false_when_no_process(self, tmp_path):
        r = _make_windows_recorder(tmp_path / "psr.exe")
        mock_result = MagicMock()
        mock_result.stdout = "INFO: No tasks are running which match the specified criteria."
        with patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result):
            assert r.is_recording_active() is False

    def test_false_when_tasklist_missing(self, tmp_path):
        r = _make_windows_recorder(tmp_path / "psr.exe")
        with patch("psr_ai.core.recorder.subprocess.run", side_effect=FileNotFoundError):
            assert r.is_recording_active() is False


# ---------------------------------------------------------------------------
# WindowsRecorder: start_recording / stop_recording
# ---------------------------------------------------------------------------

class TestWindowsRecording:

    @pytest.mark.skipif(sys.platform != "win32", reason="PSR is Windows-only")
    def test_start_creates_output_parent(self, tmp_path):
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        r = _make_windows_recorder(fake_psr)
        output = tmp_path / "sessions" / "recording.zip"
        with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
            result = r.start_recording(output_path=output)
        assert result == output.resolve()
        assert output.parent.exists()
        mock_popen.assert_called_once()
        args_passed = mock_popen.call_args[0][0]
        assert "/start" in args_passed
        assert "/output" in args_passed

    def test_start_filename_has_no_colons(self, tmp_path):
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        r = _make_windows_recorder(fake_psr)
        with patch("psr_ai.core.recorder.subprocess.Popen"):
            result = r.start_recording()
        assert ":" not in result.name

    def test_stop_returns_pending_output(self, tmp_path):
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        r = _make_windows_recorder(fake_psr)
        expected = tmp_path / "recording.zip"
        r._pending_output = expected
        with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
            result = r.stop_recording()
        assert result == expected
        mock_popen.assert_called_once()
        args_passed = mock_popen.call_args[0][0]
        assert "/stop" in args_passed


# ---------------------------------------------------------------------------
# MacRecorder
# ---------------------------------------------------------------------------

class TestMacRecorder:

    def test_is_recording_active_false_before_start(self):
        r = MacRecorder()
        assert r.is_recording_active() is False

    def test_start_creates_output_directory(self, tmp_path):
        r = MacRecorder()
        out_dir = tmp_path / "mac_session"
        mock_t = MagicMock()
        mock_t.is_alive.return_value = False
        import sys as _sys
        # Prevent pynput from loading (its Win32 backend is incompatible with mocks).
        with (
            patch("psr_ai.core.recorder.subprocess.run"),
            patch("psr_ai.core.recorder.threading.Thread", return_value=mock_t),
            patch.dict(_sys.modules, {"pynput": None, "pynput.mouse": None}),
        ):
            result = r.start_recording(output_path=out_dir, max_screenshots=1)
        assert result == out_dir
        assert out_dir.is_dir()
        assert (out_dir / "screenshots").is_dir()

    def test_stop_produces_zip(self, tmp_path):
        """stop_recording must return a ZIP path, not a raw directory."""
        staging = tmp_path / "psr_20240101_120000.d"
        staging.mkdir()
        (staging / "screenshots").mkdir()
        r = MacRecorder()
        r._output_dir = staging
        r._steps = [{"index": 0, "label": "Step 0"}]
        r._active = False
        r._thread = None
        result = r.stop_recording()
        assert result is not None
        assert result.suffix == ".zip"
        assert result.exists()
        assert zipfile.is_zipfile(result)

    def test_stop_zip_contains_data_json(self, tmp_path):
        """The produced ZIP must contain data.json with the correct step count."""
        staging = tmp_path / "session.d"
        staging.mkdir()
        (staging / "screenshots").mkdir()
        r = MacRecorder()
        r._output_dir = staging
        r._steps = [{"index": 0, "label": "Step 0"}]
        r._active = False
        r._thread = None
        zip_path = r.stop_recording()
        with zipfile.ZipFile(zip_path) as zf:
            data = json.loads(zf.read("data.json").decode("utf-8"))
        assert data["step_count"] == 1

    def test_stop_returns_none_when_not_started(self):
        r = MacRecorder()
        result = r.stop_recording()
        assert result is None

    def test_capture_step_appends_to_steps(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        r._step_index = 0
        r._steps = []
        (tmp_path / "screenshots").mkdir()
        with (
            patch("psr_ai.core.recorder.subprocess.run"),
        ):
            r._capture_step(click_x=100, click_y=200)
        assert len(r._steps) == 1
        assert r._steps[0]["click_x"] == 100
        assert r._steps[0]["click_y"] == 200

    def test_capture_step_increments_index(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        r._step_index = 0
        r._steps = []
        (tmp_path / "screenshots").mkdir()
        with patch("psr_ai.core.recorder.subprocess.run"):
            r._capture_step()
            r._capture_step()
        assert r._step_index == 2

    def test_on_click_ignored_when_not_pressed(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        r._active = True
        r._step_index = 0
        r._steps = []
        r._max = 10
        (tmp_path / "screenshots").mkdir()
        r._on_click(x=0, y=0, button=None, pressed=False)
        assert r._step_index == 0  # No capture triggered.

    def test_on_click_ignored_when_not_active(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        r._active = False
        r._step_index = 0
        r._steps = []
        r._max = 10
        (tmp_path / "screenshots").mkdir()
        r._on_click(x=0, y=0, button=None, pressed=True)
        assert r._step_index == 0

    def test_on_click_ignored_when_max_reached(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        r._active = True
        r._step_index = 5
        r._steps = []
        r._max = 5
        (tmp_path / "screenshots").mkdir()
        r._on_click(x=0, y=0, button=None, pressed=True)
        assert r._step_index == 5  # No capture beyond max.

    def test_implements_base_recorder(self):
        r = MacRecorder()
        assert isinstance(r, BaseRecorder)

    def test_backward_compat_aliases(self):
        r = MacRecorder()
        # Never started: _output_dir is None, so stop() must return None.
        out = r.stop()
        assert out is None


# ---------------------------------------------------------------------------
# LinuxRecorder
# ---------------------------------------------------------------------------

class TestLinuxRecorder:

    def test_is_recording_active_false_before_start(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        assert r.is_recording_active() is False

    def test_resolves_gnome_screenshot_first(self):
        def fake_which(name):
            return "/usr/bin/gnome-screenshot" if name == "gnome-screenshot" else None
        with patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which):
            r = LinuxRecorder()
        assert r._screenshot_cmd is not None
        assert "gnome-screenshot" in r._screenshot_cmd[0]

    def test_resolves_scrot_when_gnome_absent(self):
        def fake_which(name):
            return "/usr/bin/scrot" if name == "scrot" else None
        with patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which):
            r = LinuxRecorder()
        assert r._screenshot_cmd is not None
        assert "scrot" in r._screenshot_cmd[0]

    def test_resolves_maim_as_tier3(self):
        def fake_which(name):
            return "/usr/bin/maim" if name == "maim" else None
        with patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which):
            r = LinuxRecorder()
        assert r._screenshot_cmd is not None
        assert "maim" in r._screenshot_cmd[0]

    def test_screenshot_cmd_none_when_no_tool_found(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        assert r._screenshot_cmd is None

    def test_start_creates_output_directory(self, tmp_path):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        out_dir = tmp_path / "linux_session"
        mock_t = MagicMock()
        mock_t.is_alive.return_value = False
        import sys as _sys
        with (
            patch("psr_ai.core.recorder.threading.Thread", return_value=mock_t),
            patch.dict(_sys.modules, {"pynput": None, "pynput.mouse": None}),
        ):
            result = r.start_recording(output_path=out_dir, max_screenshots=1)
        assert result == out_dir
        assert out_dir.is_dir()
        assert (out_dir / "screenshots").is_dir()

    def test_stop_produces_zip(self, tmp_path):
        """stop_recording must return a ZIP path, not a raw directory."""
        staging = tmp_path / "psr_20240101_120000.d"
        staging.mkdir()
        (staging / "screenshots").mkdir()
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        r._output_dir = staging
        r._steps = [{"index": 0, "label": "Step 0"}]
        r._active = False
        r._thread = None
        result = r.stop_recording()
        assert result is not None
        assert result.suffix == ".zip"
        assert result.exists()
        assert zipfile.is_zipfile(result)

    def test_stop_zip_contains_data_json(self, tmp_path):
        """The produced ZIP must contain data.json with the correct step count."""
        staging = tmp_path / "session.d"
        staging.mkdir()
        (staging / "screenshots").mkdir()
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        r._output_dir = staging
        r._steps = [{"index": 0, "label": "Step 0"}]
        r._active = False
        r._thread = None
        zip_path = r.stop_recording()
        with zipfile.ZipFile(zip_path) as zf:
            data = json.loads(zf.read("data.json").decode("utf-8"))
        assert data["step_count"] == 1

    def test_stop_returns_none_when_not_started(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        result = r.stop_recording()
        assert result is None

    def test_capture_step_appends_to_steps(self, tmp_path):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        r._output_dir = tmp_path
        r._step_index = 0
        r._steps = []
        (tmp_path / "screenshots").mkdir()
        with patch("psr_ai.core.recorder.subprocess.run"):
            r._capture_step(click_x=50, click_y=75)
        assert len(r._steps) == 1
        assert r._steps[0]["click_x"] == 50

    def test_capture_step_path_template_applied(self, tmp_path):
        """The {path} placeholder in the screenshot command must be substituted."""
        def fake_which(name):
            return "/usr/bin/scrot" if name == "scrot" else None
        with patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which):
            r = LinuxRecorder()
        r._output_dir = tmp_path
        r._step_index = 0
        r._steps = []
        (tmp_path / "screenshots").mkdir()
        captured_cmds = []
        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return MagicMock(stdout="", returncode=0)
        with patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run):
            r._capture_step()
        # At least one subprocess.run call should have happened for the screenshot.
        assert any("scrot" in " ".join(c) for c in captured_cmds if isinstance(c, list))

    def test_active_window_name_returns_string(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        name = r._active_window_name()
        assert isinstance(name, str)

    def test_implements_base_recorder(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        assert isinstance(r, BaseRecorder)

    def test_backward_compat_aliases(self):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            r = LinuxRecorder()
        # Never started: _output_dir is None, so stop() must return None.
        out = r.stop()
        assert out is None


# ---------------------------------------------------------------------------
# Cross-platform: factory simulation (platform-agnostic)
# ---------------------------------------------------------------------------

class TestCrossPlatformFactory:

    def test_factory_win32_raises_psr_not_found_when_all_fail(self, tmp_path):
        with (
            patch("psr_ai.core.recorder.sys.platform", "win32"),
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            with pytest.raises(PSRNotFoundError):
                Recorder()

    def test_factory_darwin_never_raises_psr_not_found(self):
        with patch("psr_ai.core.recorder.sys.platform", "darwin"):
            r = Recorder()
        assert isinstance(r, MacRecorder)

    def test_factory_linux_never_raises_psr_not_found(self):
        with (
            patch("psr_ai.core.recorder.sys.platform", "linux"),
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
        ):
            r = Recorder()
        assert isinstance(r, LinuxRecorder)

    def test_all_recorders_have_start_stop_is_active(self):
        engines = [
            MacRecorder(),
        ]
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            engines.append(LinuxRecorder())
        for r in engines:
            assert hasattr(r, "start_recording")
            assert hasattr(r, "stop_recording")
            assert hasattr(r, "is_recording_active")
            assert hasattr(r, "start")
            assert hasattr(r, "stop")


# ---------------------------------------------------------------------------
# _pack_to_zip helper
# ---------------------------------------------------------------------------

class TestPackToZip:

    def test_zip_contains_data_json(self, tmp_path):
        asset_dir = tmp_path / "session.d"
        asset_dir.mkdir()
        (asset_dir / "screenshots").mkdir()
        (asset_dir / "data.json").write_text('{"step_count": 2}', encoding="utf-8")
        zip_path = tmp_path / "session.zip"
        result = _pack_to_zip(asset_dir, zip_path)
        with zipfile.ZipFile(result) as zf:
            assert "data.json" in zf.namelist()

    def test_zip_contains_screenshots(self, tmp_path):
        asset_dir = tmp_path / "session.d"
        asset_dir.mkdir()
        ss = asset_dir / "screenshots"
        ss.mkdir()
        (ss / "step_000.png").write_bytes(b"\x89PNG")
        (ss / "step_001.png").write_bytes(b"\x89PNG")
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "session.zip"
        _pack_to_zip(asset_dir, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "screenshots/step_000.png" in names
        assert "screenshots/step_001.png" in names

    def test_asset_dir_removed_after_packing(self, tmp_path):
        asset_dir = tmp_path / "session.d"
        asset_dir.mkdir()
        (asset_dir / "screenshots").mkdir()
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "session.zip"
        _pack_to_zip(asset_dir, zip_path)
        assert not asset_dir.exists()

    def test_returns_zip_path(self, tmp_path):
        asset_dir = tmp_path / "s.d"
        asset_dir.mkdir()
        (asset_dir / "screenshots").mkdir()
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "s.zip"
        result = _pack_to_zip(asset_dir, zip_path)
        assert result == zip_path

    def test_empty_screenshots_dir_produces_valid_zip(self, tmp_path):
        asset_dir = tmp_path / "empty.d"
        asset_dir.mkdir()
        (asset_dir / "screenshots").mkdir()
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "empty.zip"
        result = _pack_to_zip(asset_dir, zip_path)
        assert zipfile.is_zipfile(result)

    def test_missing_data_json_still_produces_zip(self, tmp_path):
        """data.json may not exist if recording produced zero steps."""
        asset_dir = tmp_path / "nodatajson.d"
        asset_dir.mkdir()
        (asset_dir / "screenshots").mkdir()
        zip_path = tmp_path / "nodatajson.zip"
        result = _pack_to_zip(asset_dir, zip_path)
        assert result.exists()
        assert zipfile.is_zipfile(result)

    def test_non_png_files_excluded(self, tmp_path):
        asset_dir = tmp_path / "mixed.d"
        asset_dir.mkdir()
        ss = asset_dir / "screenshots"
        ss.mkdir()
        (ss / "step_000.png").write_bytes(b"\x89PNG")
        (ss / "thumbs.db").write_bytes(b"\x00\x00")  # Should be ignored.
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "mixed.zip"
        _pack_to_zip(asset_dir, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "screenshots/thumbs.db" not in names
        assert "screenshots/step_000.png" in names


# ---------------------------------------------------------------------------
# MacRecorder: idle-frame deduplication
# ---------------------------------------------------------------------------

class TestMacRecorderIdleDedup:

    def test_is_idle_true_when_state_matches_last(self):
        r = MacRecorder()
        r._last_program = "Firefox"
        r._last_x = 0
        r._last_y = 0
        assert r._is_idle("Firefox", 0, 0) is True

    def test_is_idle_false_when_program_changes(self):
        r = MacRecorder()
        r._last_program = "Firefox"
        r._last_x = 0
        r._last_y = 0
        assert r._is_idle("Terminal", 0, 0) is False

    def test_is_idle_false_on_first_check(self):
        """Before any capture, sentinel values (-1, -1) ensure the first frame is never idle."""
        r = MacRecorder()
        # Coordinates of (0, 0) differ from sentinels (-1, -1) so not idle.
        assert r._is_idle("", 0, 0) is False

    def test_interval_capture_skips_idle_frame(self, tmp_path):
        """When the program and coords have not changed, no step is appended."""
        r = MacRecorder()
        r._output_dir = tmp_path
        (tmp_path / "screenshots").mkdir()
        r._step_index = 0
        r._steps = []
        r._active = True
        r._max = 5
        r._last_program = "Chrome"
        r._last_x = 0
        r._last_y = 0

        iteration = [0]

        def fake_frontmost():
            iteration[0] += 1
            if iteration[0] >= 2:
                r._active = False  # Stop after second check.
            return "Chrome"  # Same program every time - idle.

        r._frontmost_app = fake_frontmost
        with patch("psr_ai.core.recorder.time.sleep"):
            r._interval_capture()

        # No step captured because every frame was idle.
        assert len(r._steps) == 0

    def test_interval_capture_captures_on_program_change(self, tmp_path):
        """A program change breaks the idle condition and triggers a capture."""
        r = MacRecorder()
        r._output_dir = tmp_path
        (tmp_path / "screenshots").mkdir()
        r._step_index = 0
        r._steps = []
        r._active = True
        r._max = 5
        r._last_program = "Chrome"
        r._last_x = 0
        r._last_y = 0

        programs = iter(["Terminal", "Terminal"])

        def fake_frontmost():
            try:
                prog = next(programs)
            except StopIteration:
                r._active = False
                prog = "Terminal"
            return prog

        r._frontmost_app = fake_frontmost

        with (
            patch("psr_ai.core.recorder.subprocess.run"),
            patch("psr_ai.core.recorder.time.sleep"),
        ):
            r._interval_capture()

        assert len(r._steps) >= 1
        assert r._steps[0]["program"] == "Terminal"

    def test_capture_step_updates_dedup_state(self, tmp_path):
        r = MacRecorder()
        r._output_dir = tmp_path
        (tmp_path / "screenshots").mkdir()
        r._step_index = 0
        r._steps = []
        r._last_program = ""
        r._last_x = -1
        r._last_y = -1
        with patch("psr_ai.core.recorder.subprocess.run"):
            r._capture_step(click_x=42, click_y=99, program_hint="VSCode")
        assert r._last_program == "VSCode"
        assert r._last_x == 42
        assert r._last_y == 99


# ---------------------------------------------------------------------------
# LinuxRecorder: idle-frame deduplication
# ---------------------------------------------------------------------------

class TestLinuxRecorderIdleDedup:

    def _make(self) -> LinuxRecorder:
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            return LinuxRecorder()

    def test_is_idle_true_when_state_matches_last(self):
        r = self._make()
        r._last_program = "gedit"
        r._last_x = 0
        r._last_y = 0
        assert r._is_idle("gedit", 0, 0) is True

    def test_is_idle_false_when_program_changes(self):
        r = self._make()
        r._last_program = "gedit"
        r._last_x = 0
        r._last_y = 0
        assert r._is_idle("bash", 0, 0) is False

    def test_is_idle_false_on_first_check(self):
        r = self._make()
        assert r._is_idle("", 0, 0) is False

    def test_capture_step_updates_dedup_state(self, tmp_path):
        r = self._make()
        r._output_dir = tmp_path
        (tmp_path / "screenshots").mkdir()
        r._step_index = 0
        r._steps = []
        r._last_program = ""
        r._last_x = -1
        r._last_y = -1
        with patch("psr_ai.core.recorder.subprocess.run"):
            r._capture_step(click_x=10, click_y=20, program_hint="nano")
        assert r._last_program == "nano"
        assert r._last_x == 10
        assert r._last_y == 20


# ---------------------------------------------------------------------------
# LinuxRecorder: Wayland / X11 display server detection
# ---------------------------------------------------------------------------

class TestLinuxWaylandDetection:

    def _make(self) -> LinuxRecorder:
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            return LinuxRecorder()

    def test_gdbus_tried_first_when_wayland_display_set(self):
        r = self._make()
        call_order: list[str] = []

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in ("gdbus", "xprop") else None

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "gdbus" in cmd[0]:
                call_order.append("gdbus")
                result.stdout = '(true, "Wayland App")'
            elif "xprop" in cmd[0]:
                call_order.append("xprop")
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with (
            patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which),
            patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run),
            patch("psr_ai.core.recorder.os.environ.get", return_value="wayland-0"),
        ):
            name = r._active_window_name()

        assert call_order[0] == "gdbus"
        assert name == "Wayland App"

    def test_xprop_tried_first_when_wayland_display_absent(self):
        r = self._make()
        call_order: list[str] = []

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in ("gdbus", "xprop") else None

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "xprop" in cmd[0]:
                call_order.append("xprop")
                if cmd == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
                    result.stdout = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x1"
                else:
                    result.stdout = '_NET_WM_NAME(UTF8_STRING) = "X11 App"'
            elif "gdbus" in cmd[0]:
                call_order.append("gdbus")
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with (
            patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which),
            patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run),
            patch("psr_ai.core.recorder.os.environ.get", return_value=None),
        ):
            name = r._active_window_name()

        assert call_order[0] == "xprop"
        assert name == "X11 App"

    def test_falls_back_to_xprop_when_gdbus_returns_empty_on_wayland(self):
        r = self._make()

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in ("gdbus", "xprop") else None

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "gdbus" in cmd[0]:
                result.stdout = ""
            elif "xprop" in cmd[0]:
                if cmd == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
                    result.stdout = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2"
                else:
                    result.stdout = '_NET_WM_NAME(UTF8_STRING) = "XWayland App"'
            else:
                result.stdout = ""
            return result

        with (
            patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which),
            patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run),
            patch("psr_ai.core.recorder.os.environ.get", return_value="wayland-0"),
        ):
            name = r._active_window_name()

        assert name == "XWayland App"

    def test_returns_empty_when_both_backends_fail(self):
        r = self._make()

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in ("gdbus", "xprop") else None

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            return result

        with (
            patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which),
            patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run),
            patch("psr_ai.core.recorder.os.environ.get", return_value=None),
        ):
            name = r._active_window_name()

        assert name == ""
