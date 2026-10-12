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
    WindowsPollingRecorder,
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
        # Keep which() mocked here too: on hosts with gdbus or xprop (CI runners)
        # the window-title lookup would otherwise parse a MagicMock's stdout.
        with (
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.subprocess.run"),
        ):
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

    def test_factory_win32_falls_back_to_polling_when_psr_missing(self, tmp_path):
        with (
            patch("psr_ai.core.recorder.sys.platform", "win32"),
            patch("psr_ai.core.recorder.shutil.which", return_value=None),
            patch("psr_ai.core.recorder.resolve_env_path", side_effect=KeyError),
            patch("psr_ai.core.recorder._resolve_bundled_asset_dir", return_value=tmp_path),
        ):
            r = Recorder()
        assert isinstance(r, WindowsPollingRecorder)

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


# ---------------------------------------------------------------------------
# Polling session: memory guard, visual diffing, cross-process stop
# ---------------------------------------------------------------------------

from psr_ai.core import recorder as _recorder_mod
from psr_ai.core.recorder import (  # noqa: E402
    _DEFAULT_MAX_SCREENSHOTS,
    _PollingSession,
    _STOP_SENTINEL,
    run_polling_session,
)

PIL = pytest.importorskip("PIL", reason="visual diffing needs Pillow")


def _solid(shade: int):
    from PIL import Image
    return Image.new("RGB", (160, 100), (shade, shade, shade))


class TestPollingSessionMemoryGuard:

    def test_default_cap_is_100(self):
        assert _DEFAULT_MAX_SCREENSHOTS == 100

    def test_session_stops_itself_at_the_cap(self, tmp_path):
        staging, zip_path = tmp_path / "s.d", tmp_path / "s.zip"
        shades = iter(range(5, 255, 20))

        with (
            patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(next(shades))),
            patch.object(_recorder_mod.time, "sleep"),
        ):
            result = run_polling_session(staging, zip_path, max_screenshots=4, interval=0.01)

        with zipfile.ZipFile(result) as zf:
            data = json.loads(zf.read("data.json"))
            frames = [n for n in zf.namelist() if n.endswith(".png")]
        assert data["step_count"] == 4
        assert len(frames) == 4

    def test_cap_is_never_exceeded_by_extra_captures(self, tmp_path):
        shades = iter(range(5, 255, 7))
        session = _PollingSession(tmp_path / "s.d", tmp_path / "s.zip", max_screenshots=2)
        (tmp_path / "s.d" / "screenshots").mkdir(parents=True)
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(next(shades))):
            saved = [session.capture() for _ in range(6)]
        assert saved.count(True) == 2
        assert session.should_stop() is True

    def test_staging_directory_is_removed_after_packing(self, tmp_path):
        staging, zip_path = tmp_path / "s.d", tmp_path / "s.zip"
        shades = iter(range(5, 255, 30))
        with (
            patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(next(shades))),
            patch.object(_recorder_mod.time, "sleep"),
        ):
            run_polling_session(staging, zip_path, max_screenshots=2, interval=0.01)
        assert not staging.exists()
        assert zip_path.exists()


class TestPollingSessionVisualDiffing:

    def test_identical_frames_are_never_written(self, tmp_path):
        staging = tmp_path / "s.d"
        (staging / "screenshots").mkdir(parents=True)
        session = _PollingSession(staging, tmp_path / "s.zip", max_screenshots=50)

        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(40)):
            first = session.capture()
            repeats = [session.capture() for _ in range(5)]

        assert first is True
        assert repeats == [False] * 5
        assert len(list((staging / "screenshots").glob("*.png"))) == 1

    def test_a_changed_screen_produces_a_new_frame(self, tmp_path):
        staging = tmp_path / "s.d"
        (staging / "screenshots").mkdir(parents=True)
        session = _PollingSession(staging, tmp_path / "s.zip", max_screenshots=50)

        shades = iter([30, 30, 30, 220])
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(next(shades))):
            saved = [session.capture() for _ in range(4)]

        assert saved == [True, False, False, True]
        assert len(list((staging / "screenshots").glob("*.png"))) == 2

    def test_step_indices_stay_contiguous_after_discards(self, tmp_path):
        staging = tmp_path / "s.d"
        (staging / "screenshots").mkdir(parents=True)
        session = _PollingSession(staging, tmp_path / "s.zip", max_screenshots=50)

        shades = iter([10, 10, 120, 120, 240])
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(next(shades))):
            for _ in range(5):
                session.capture()
        zip_path = session.finalize()

        with zipfile.ZipFile(zip_path) as zf:
            data = json.loads(zf.read("data.json"))
            names = sorted(n for n in zf.namelist() if n.endswith(".png"))
        assert [s["index"] for s in data["steps"]] == [0, 1, 2]
        assert names == [
            "screenshots/step_000.png",
            "screenshots/step_001.png",
            "screenshots/step_002.png",
        ]

    def test_max_dim_downscales_saved_frames(self, tmp_path):
        from PIL import Image
        staging = tmp_path / "s.d"
        (staging / "screenshots").mkdir(parents=True)
        session = _PollingSession(staging, tmp_path / "s.zip", max_screenshots=5, max_dim=64)
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(80)):
            session.capture()
        png = next((staging / "screenshots").glob("*.png"))
        with Image.open(png) as img:
            assert max(img.width, img.height) == 64


class TestPollingSessionStopSentinel:

    def test_sentinel_ends_the_loop(self, tmp_path):
        staging, zip_path = tmp_path / "s.d", tmp_path / "s.zip"
        (staging / "screenshots").mkdir(parents=True)
        (staging / _STOP_SENTINEL).write_text("stop", encoding="utf-8")

        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(10)):
            result = run_polling_session(staging, zip_path, max_screenshots=100, interval=0.01)

        with zipfile.ZipFile(result) as zf:
            data = json.loads(zf.read("data.json"))
        assert data["step_count"] == 0
        assert result.exists()

    def test_sentinel_is_not_packed_into_the_zip(self, tmp_path):
        staging, zip_path = tmp_path / "s.d", tmp_path / "s.zip"
        (staging / "screenshots").mkdir(parents=True)
        (staging / _STOP_SENTINEL).write_text("stop", encoding="utf-8")
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(10)):
            result = run_polling_session(staging, zip_path, max_screenshots=5, interval=0.01)
        with zipfile.ZipFile(result) as zf:
            assert _STOP_SENTINEL not in zf.namelist()

    def test_capture_survives_a_missing_staging_directory(self, tmp_path):
        session = _PollingSession(tmp_path / "gone.d", tmp_path / "gone.zip", max_screenshots=3)
        with patch.object(_recorder_mod, "_grab_screen_image", lambda: _solid(60)):
            assert session.capture() is True


# ---------------------------------------------------------------------------
# WindowsPollingRecorder: detached daemon lifecycle
# ---------------------------------------------------------------------------

class TestWindowsPollingRecorderLifecycle:

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            WindowsPollingRecorder, "_STATE_FILE", tmp_path / "state.json", raising=False
        )
        yield

    def test_start_spawns_a_daemon_and_returns_a_zip_path(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            out = rec.start_recording(output_path=tmp_path / "sess.zip", max_screenshots=7)
        assert out == (tmp_path / "sess.zip").resolve()
        assert (tmp_path / "sess.d" / "screenshots").is_dir()

    def test_start_records_session_state_for_other_processes(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            rec.start_recording(output_path=tmp_path / "sess.zip", max_screenshots=7)
        state = WindowsPollingRecorder._load_state()
        assert state["pid"] == 4242
        assert state["max_screenshots"] == 7
        assert Path(state["zip"]).name == "sess.zip"

    def test_start_clears_a_stale_sentinel(self, tmp_path):
        staging = tmp_path / "sess.d"
        (staging / "screenshots").mkdir(parents=True)
        (staging / _STOP_SENTINEL).write_text("stop", encoding="utf-8")
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=1):
            rec.start_recording(output_path=tmp_path / "sess.zip")
        assert not (staging / _STOP_SENTINEL).exists()

    def test_stop_writes_the_sentinel_for_the_daemon(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            rec.start_recording(output_path=tmp_path / "sess.zip")
        staging = tmp_path / "sess.d"

        # A dead daemon leaves the salvage path to pack whatever exists.
        with patch.object(WindowsPollingRecorder, "_pid_alive", staticmethod(lambda pid: False)):
            result = rec.stop_recording()

        assert result == tmp_path / "sess.zip"
        assert result.exists()
        assert zipfile.is_zipfile(result)
        assert not staging.exists()

    def test_stop_salvages_frames_from_a_dead_daemon(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            rec.start_recording(output_path=tmp_path / "sess.zip")
        shots = tmp_path / "sess.d" / "screenshots"
        (shots / "step_000.png").write_bytes(b"\x89PNG")
        (shots / "step_001.png").write_bytes(b"\x89PNG")

        with patch.object(WindowsPollingRecorder, "_pid_alive", staticmethod(lambda pid: False)):
            result = rec.stop_recording()

        with zipfile.ZipFile(result) as zf:
            data = json.loads(zf.read("data.json"))
        assert data["step_count"] == 2

    def test_stop_clears_state_so_status_reads_idle(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            rec.start_recording(output_path=tmp_path / "sess.zip")
        with patch.object(WindowsPollingRecorder, "_pid_alive", staticmethod(lambda pid: False)):
            rec.stop_recording()
        assert WindowsPollingRecorder._load_state() is None
        assert WindowsPollingRecorder.has_active_session() is False

    def test_stop_without_a_session_returns_none(self):
        assert WindowsPollingRecorder().stop_recording() is None

    def test_has_active_session_true_while_the_daemon_lives(self, tmp_path):
        rec = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            rec.start_recording(output_path=tmp_path / "sess.zip")
        with patch.object(WindowsPollingRecorder, "_pid_alive", staticmethod(lambda pid: True)):
            assert WindowsPollingRecorder.has_active_session() is True

    def test_pid_alive_false_for_a_bogus_pid(self):
        assert WindowsPollingRecorder._pid_alive(None) is False
        assert WindowsPollingRecorder._pid_alive(0) is False


# ---------------------------------------------------------------------------
# Windows: silent fallback when psr.exe is blocked
# ---------------------------------------------------------------------------

class TestWindowsBlockedPsrFallback:

    def _recorder(self, tmp_path) -> WindowsRecorder:
        rec = _make_windows_recorder(tmp_path / "psr.exe")
        rec._fallback = None
        return rec

    def test_permission_error_falls_back_to_polling(self, tmp_path):
        rec = self._recorder(tmp_path)
        with (
            patch("psr_ai.core.recorder.subprocess.Popen",
                  side_effect=PermissionError(5, "Access is denied")),
            patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=99),
            patch.object(WindowsPollingRecorder, "_STATE_FILE", tmp_path / "state.json"),
        ):
            out = rec.start_recording(output_path=tmp_path / "sess.zip", max_screenshots=5)
        assert isinstance(rec._fallback, WindowsPollingRecorder)
        assert out == (tmp_path / "sess.zip").resolve()

    def test_group_policy_block_falls_back_to_polling(self, tmp_path):
        rec = self._recorder(tmp_path)
        blocked = OSError("This program is blocked by group policy")
        blocked.winerror = 1260
        with (
            patch("psr_ai.core.recorder.subprocess.Popen", side_effect=blocked),
            patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=99),
            patch.object(WindowsPollingRecorder, "_STATE_FILE", tmp_path / "state.json"),
        ):
            out = rec.start_recording(output_path=tmp_path / "sess.zip")
        assert isinstance(rec._fallback, WindowsPollingRecorder)
        assert out.suffix == ".zip"

    def test_fallback_receives_the_caller_settings(self, tmp_path):
        rec = self._recorder(tmp_path)
        seen = {}

        def fake_spawn(self, staging, zip_path, maxsc, max_dim):
            seen.update(staging=staging, zip=zip_path, maxsc=maxsc, max_dim=max_dim)
            return 7

        with (
            patch("psr_ai.core.recorder.subprocess.Popen", side_effect=PermissionError),
            patch.object(WindowsPollingRecorder, "_spawn_daemon", fake_spawn),
            patch.object(WindowsPollingRecorder, "_STATE_FILE", tmp_path / "state.json"),
        ):
            rec.start_recording(output_path=tmp_path / "sess.zip", max_screenshots=13, max_dim=512)
        assert seen["maxsc"] == 13
        assert seen["max_dim"] == 512
        assert seen["staging"] == (tmp_path / "sess.d").resolve()

    def test_stop_is_delegated_to_the_fallback(self, tmp_path):
        rec = self._recorder(tmp_path)
        fallback = MagicMock()
        fallback.stop_recording.return_value = tmp_path / "sess.zip"
        rec._fallback = fallback
        assert rec.stop_recording() == tmp_path / "sess.zip"
        fallback.stop_recording.assert_called_once()

    def test_is_recording_active_is_delegated_to_the_fallback(self, tmp_path):
        rec = self._recorder(tmp_path)
        fallback = MagicMock()
        fallback.is_recording_active.return_value = True
        rec._fallback = fallback
        assert rec.is_recording_active() is True

    def test_factory_prefers_a_live_polling_session(self, tmp_path):
        with (
            patch("psr_ai.core.recorder.sys.platform", "win32"),
            patch.object(WindowsPollingRecorder, "has_active_session", return_value=True),
            patch("psr_ai.core.recorder.shutil.which", return_value=str(tmp_path / "psr.exe")),
        ):
            assert isinstance(Recorder(), WindowsPollingRecorder)

    def test_factory_falls_back_when_construction_raises_permission_error(self, tmp_path):
        with (
            patch("psr_ai.core.recorder.sys.platform", "win32"),
            patch.object(WindowsPollingRecorder, "has_active_session", return_value=False),
            patch("psr_ai.core.recorder.shutil.which", side_effect=PermissionError),
        ):
            assert isinstance(Recorder(), WindowsPollingRecorder)


# ---------------------------------------------------------------------------
# Memory guard on the macOS / Linux engines
# ---------------------------------------------------------------------------

class TestClickEngineMemoryGuard:

    def _prepare(self, rec, tmp_path, max_screenshots: int):
        rec._output_dir = tmp_path
        (tmp_path / "screenshots").mkdir(parents=True, exist_ok=True)
        rec._step_index = 0
        rec._steps = []
        rec._active = True
        rec._max = max_screenshots
        rec._finalized_zip = None
        return rec

    def test_mac_session_finalizes_itself_at_the_cap(self, tmp_path):
        rec = self._prepare(MacRecorder(), tmp_path / "sess.d", 2)
        with patch("psr_ai.core.recorder.subprocess.run", return_value=MagicMock(stdout="Finder")):
            rec._on_click(1, 1, None, True)
            rec._on_click(2, 2, None, True)
        assert rec._finalized_zip is not None
        assert rec._finalized_zip.exists()
        assert rec._active is False

    def test_mac_stop_returns_the_auto_packed_zip_without_repacking(self, tmp_path):
        rec = self._prepare(MacRecorder(), tmp_path / "sess.d", 1)
        with patch("psr_ai.core.recorder.subprocess.run", return_value=MagicMock(stdout="Finder")):
            rec._on_click(1, 1, None, True)
        auto = rec._finalized_zip
        assert rec.stop_recording() == auto

    def test_linux_session_finalizes_itself_at_the_cap(self, tmp_path):
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            rec = LinuxRecorder()
        self._prepare(rec, tmp_path / "sess.d", 1)
        with patch("psr_ai.core.recorder.subprocess.run", return_value=MagicMock(stdout="Finder")):
            rec._on_click(3, 4, None, True)
        assert rec._finalized_zip is not None
        assert rec._finalized_zip.exists()
        assert rec._active is False

    def test_clicks_after_the_cap_do_not_add_steps(self, tmp_path):
        rec = self._prepare(MacRecorder(), tmp_path / "sess.d", 1)
        with patch("psr_ai.core.recorder.subprocess.run", return_value=MagicMock(stdout="Finder")):
            rec._on_click(1, 1, None, True)
            rec._on_click(2, 2, None, True)
        assert len(rec._steps) == 1

    def test_default_max_screenshots_is_100_on_every_engine(self, tmp_path):
        import inspect
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            engines = [MacRecorder(), LinuxRecorder(), WindowsPollingRecorder()]
        for engine in engines:
            signature = inspect.signature(engine.start_recording)
            assert signature.parameters["max_screenshots"].default == 100


class TestPackToZipAtomicity:

    def test_zip_is_published_atomically(self, tmp_path):
        asset_dir = tmp_path / "s.d"
        (asset_dir / "screenshots").mkdir(parents=True)
        (asset_dir / "data.json").write_text("{}", encoding="utf-8")
        zip_path = tmp_path / "s.zip"
        _pack_to_zip(asset_dir, zip_path)
        assert zipfile.is_zipfile(zip_path)
        assert not (tmp_path / "s.zip.part").exists()

    def test_existing_zip_is_replaced_not_corrupted(self, tmp_path):
        asset_dir = tmp_path / "s.d"
        (asset_dir / "screenshots").mkdir(parents=True)
        (asset_dir / "data.json").write_text('{"step_count": 1}', encoding="utf-8")
        zip_path = tmp_path / "s.zip"
        zip_path.write_bytes(b"stale contents")
        _pack_to_zip(asset_dir, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert json.loads(zf.read("data.json"))["step_count"] == 1


class TestConcurrentStartGuard:

    def test_start_stops_an_orphaned_session_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            WindowsPollingRecorder, "_STATE_FILE", tmp_path / "state.json", raising=False
        )
        first = WindowsPollingRecorder()
        with patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=4242):
            first.start_recording(output_path=tmp_path / "one.zip")

        stopped = []
        original_stop = WindowsPollingRecorder.stop_recording

        def tracking_stop(self):
            stopped.append(self._state or self._load_state())
            return original_stop(self)

        with (
            patch.object(WindowsPollingRecorder, "has_active_session", return_value=True),
            patch.object(WindowsPollingRecorder, "stop_recording", tracking_stop),
            patch.object(WindowsPollingRecorder, "_pid_alive", staticmethod(lambda pid: False)),
            patch.object(WindowsPollingRecorder, "_spawn_daemon", return_value=7),
        ):
            WindowsPollingRecorder().start_recording(output_path=tmp_path / "two.zip")

        assert len(stopped) == 1
        assert Path(stopped[0]["zip"]).name == "one.zip"
        assert WindowsPollingRecorder._load_state()["pid"] == 7
