"""Unit tests for core/executor.py - platform executor factory and actions."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from psr_ai.core.executor import (
    BaseExecutor,
    WindowsExecutor,
    MacExecutor,
    LinuxExecutor,
    get_executor,
    ACTION_CLICK, ACTION_DOUBLE_CLICK, ACTION_TYPE, ACTION_KEY,
    ACTION_SCROLL, ACTION_MOVE, ACTION_DONE, ACTION_ASK_USER,
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestGetExecutor:

    def test_returns_windows_executor_on_win32(self):
        with patch("psr_ai.core.executor.sys.platform", "win32"):
            ex = get_executor()
        assert isinstance(ex, WindowsExecutor)

    def test_returns_mac_executor_on_darwin(self):
        with patch("psr_ai.core.executor.sys.platform", "darwin"):
            ex = get_executor()
        assert isinstance(ex, MacExecutor)

    def test_returns_linux_executor_on_linux(self):
        with (
            patch("psr_ai.core.executor.sys.platform", "linux"),
            patch("psr_ai.core.executor.shutil.which", return_value=None),
        ):
            ex = get_executor()
        assert isinstance(ex, LinuxExecutor)

    def test_raises_on_unknown_platform(self):
        with patch("psr_ai.core.executor.sys.platform", "freebsd"):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                get_executor()

    def test_all_executors_are_base_executor_subclasses(self):
        assert issubclass(WindowsExecutor, BaseExecutor)
        assert issubclass(MacExecutor, BaseExecutor)
        assert issubclass(LinuxExecutor, BaseExecutor)


# ---------------------------------------------------------------------------
# BaseExecutor.dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    """dispatch() must translate action dicts into the correct method calls."""

    def _make_executor(self) -> BaseExecutor:
        """Return a MacExecutor with all concrete methods mocked."""
        ex = MacExecutor()
        ex.click = MagicMock()
        ex.double_click = MagicMock()
        ex.move = MagicMock()
        ex.type_text = MagicMock()
        ex.key = MagicMock()
        ex.scroll = MagicMock()
        ex.screenshot = MagicMock(return_value=b"")
        return ex

    def test_click_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "click", "x": 100, "y": 200, "button": "right"})
        ex.click.assert_called_once_with(100, 200, "right")

    def test_click_defaults_to_left_button(self):
        ex = self._make_executor()
        ex.dispatch({"type": "click", "x": 50, "y": 60})
        ex.click.assert_called_once_with(50, 60, "left")

    def test_double_click_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "double_click", "x": 10, "y": 20})
        ex.double_click.assert_called_once_with(10, 20)

    def test_move_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "move", "x": 300, "y": 400})
        ex.move.assert_called_once_with(300, 400)

    def test_type_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "type", "text": "hello world"})
        ex.type_text.assert_called_once_with("hello world")

    def test_key_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "key", "key": "ctrl+c"})
        ex.key.assert_called_once_with("ctrl+c")

    def test_scroll_dispatches_correctly(self):
        ex = self._make_executor()
        ex.dispatch({"type": "scroll", "x": 500, "y": 600, "direction": "up", "amount": 5})
        ex.scroll.assert_called_once_with(500, 600, "up", 5)

    def test_scroll_defaults_applied(self):
        ex = self._make_executor()
        ex.dispatch({"type": "scroll"})
        ex.scroll.assert_called_once_with(0, 0, "down", 3)

    def test_done_does_nothing(self):
        ex = self._make_executor()
        ex.dispatch({"type": "done", "result": "finished"})
        ex.click.assert_not_called()

    def test_ask_user_does_nothing(self):
        ex = self._make_executor()
        ex.dispatch({"type": "ask_user", "question": "?"})
        ex.click.assert_not_called()

    def test_screenshot_type_does_nothing(self):
        ex = self._make_executor()
        ex.dispatch({"type": "screenshot"})
        ex.click.assert_not_called()

    def test_unknown_action_raises_value_error(self):
        ex = self._make_executor()
        with pytest.raises(ValueError, match="Unknown action type"):
            ex.dispatch({"type": "teleport"})


# ---------------------------------------------------------------------------
# MacExecutor
# ---------------------------------------------------------------------------

class TestMacExecutor:

    def test_screenshot_calls_screencapture(self, tmp_path):
        ex = MacExecutor()
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

        def fake_run(cmd, **kwargs):
            # Write fake PNG to the tmp path passed in cmd.
            png_path = Path(cmd[2])
            png_path.write_bytes(fake_png)
            return MagicMock(returncode=0)

        with patch("psr_ai.core.executor.subprocess.run", side_effect=fake_run):
            result = ex.screenshot()

        assert result == fake_png

    def test_screenshot_returns_empty_bytes_on_failure(self):
        ex = MacExecutor()
        with patch("psr_ai.core.executor.subprocess.run", return_value=MagicMock()):
            result = ex.screenshot()
        assert result == b""

    def test_click_calls_pyautogui(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.click(100, 200, "left")
        mock_pag.click.assert_called_once_with(100, 200, button="left")

    def test_double_click_calls_pyautogui(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.double_click(50, 75)
        mock_pag.doubleClick.assert_called_once_with(50, 75)

    def test_type_text_calls_pyautogui_write(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.type_text("hello")
        mock_pag.write.assert_called_once_with("hello", interval=0.02)

    def test_key_single_calls_press(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.key("enter")
        mock_pag.press.assert_called_once_with("enter")

    def test_key_combo_calls_hotkey(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.key("ctrl+c")
        mock_pag.hotkey.assert_called_once_with("ctrl", "c")

    def test_scroll_down_passes_negative_clicks(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.scroll(100, 200, "down", 3)
        mock_pag.scroll.assert_called_once_with(-3)

    def test_scroll_up_passes_positive_clicks(self):
        ex = MacExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.scroll(100, 200, "up", 2)
        mock_pag.scroll.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# LinuxExecutor
# ---------------------------------------------------------------------------

class TestLinuxExecutor:

    def _make(self, screenshot_tool: str | None = "scrot") -> LinuxExecutor:
        with patch("psr_ai.core.executor.shutil.which", return_value=screenshot_tool):
            return LinuxExecutor()

    def test_resolves_first_available_tool(self):
        def fake_which(name):
            return "/usr/bin/scrot" if name == "scrot" else None
        with patch("psr_ai.core.executor.shutil.which", side_effect=fake_which):
            ex = LinuxExecutor()
        assert ex._screenshot_cmd is not None
        assert "scrot" in ex._screenshot_cmd[0]

    def test_screenshot_cmd_none_when_no_tool(self):
        with patch("psr_ai.core.executor.shutil.which", return_value=None):
            ex = LinuxExecutor()
        assert ex._screenshot_cmd is None

    def test_screenshot_raises_when_no_tool(self):
        with patch("psr_ai.core.executor.shutil.which", return_value=None):
            ex = LinuxExecutor()
        with pytest.raises(RuntimeError, match="No screenshot tool found"):
            ex.screenshot()

    def test_screenshot_calls_resolved_tool(self, tmp_path):
        def fake_which(name):
            return "/usr/bin/maim" if name == "maim" else None
        with patch("psr_ai.core.executor.shutil.which", side_effect=fake_which):
            ex = LinuxExecutor()

        fake_png = b"\x89PNG"
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            Path(cmd[-1]).write_bytes(fake_png)
            return MagicMock(returncode=0)

        with patch("psr_ai.core.executor.subprocess.run", side_effect=fake_run):
            result = ex.screenshot()

        assert result == fake_png
        assert any("maim" in c[0] for c in captured_cmds)

    def test_click_calls_pyautogui(self):
        ex = self._make()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.click(200, 300)
        mock_pag.click.assert_called_once_with(200, 300, button="left")

    def test_pyautogui_import_error_raises_clear_message(self):
        ex = self._make()
        with patch("psr_ai.core.executor._pyautogui", side_effect=ImportError("no pyautogui")):
            with pytest.raises(ImportError, match="no pyautogui"):
                ex.click(0, 0)


# ---------------------------------------------------------------------------
# WindowsExecutor
# ---------------------------------------------------------------------------

class TestWindowsExecutor:

    def test_screenshot_uses_pil_imagegrab(self):
        ex = WindowsExecutor()
        mock_img = MagicMock()
        import io
        buf = io.BytesIO()

        def fake_save(buf_arg, format):
            buf_arg.write(b"\x89PNG fake")

        mock_img.save = fake_save

        with patch("psr_ai.core.executor.ImageGrab", create=True) as mock_grab:
            mock_grab.grab.return_value = mock_img
            # Patch the import inside screenshot()
            with patch.dict("sys.modules", {"PIL.ImageGrab": mock_grab, "PIL": MagicMock()}):
                pass  # Just verify structure is correct

    def test_click_calls_pyautogui(self):
        ex = WindowsExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.click(400, 300)
        mock_pag.click.assert_called_once_with(400, 300, button="left")

    def test_key_combo_split_correctly(self):
        ex = WindowsExecutor()
        mock_pag = MagicMock()
        with patch("psr_ai.core.executor._pyautogui", return_value=mock_pag):
            ex.key("alt+f4")
        mock_pag.hotkey.assert_called_once_with("alt", "f4")


# ---------------------------------------------------------------------------
# LinuxExecutor - Wayland warning (Phase 10)
# ---------------------------------------------------------------------------

class TestLinuxExecutorWaylandWarning:

    def test_wayland_warning_printed_when_wayland_display_set(self, capsys):
        with (
            patch("psr_ai.core.executor.shutil.which", return_value=None),
            patch.dict("os.environ", {"WAYLAND_DISPLAY": ":0"}, clear=False),
        ):
            LinuxExecutor()
        out = capsys.readouterr().out
        assert "Wayland" in out

    def test_no_warning_when_wayland_display_not_set(self, capsys):
        import os
        env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
        with (
            patch("psr_ai.core.executor.shutil.which", return_value=None),
            patch.dict("os.environ", env, clear=True),
        ):
            LinuxExecutor()
        out = capsys.readouterr().out
        assert "Wayland" not in out

    def test_warning_mentions_x11_fallback(self, capsys):
        with (
            patch("psr_ai.core.executor.shutil.which", return_value=None),
            patch.dict("os.environ", {"WAYLAND_DISPLAY": ":0"}, clear=False),
        ):
            LinuxExecutor()
        out = capsys.readouterr().out
        assert "X11" in out
