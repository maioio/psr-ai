"""Desktop action executor: platform factory that translates agent decisions into OS input.

The executor is the only module in PSR.ai that writes mouse and keyboard events.
All other modules are read-only (capture, analyze, publish).

Platform dispatch mirrors recorder.py:
  win32  -> WindowsExecutor
  darwin -> MacExecutor
  linux* -> LinuxExecutor

Dependencies are optional and loaded lazily so the module imports cleanly even
when pyautogui is not installed. Missing dependencies raise ImportError with a
clear install hint pointing to the [agent] extra.

Usage:
    from psr_ai.core.executor import get_executor
    ex = get_executor()
    img_bytes = ex.screenshot()
    ex.click(400, 300)
    ex.type_text("hello world")
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path


# ---------------------------------------------------------------------------
# Action type constants
# ---------------------------------------------------------------------------

ACTION_CLICK        = "click"
ACTION_DOUBLE_CLICK = "double_click"
ACTION_TYPE         = "type"
ACTION_KEY          = "key"
ACTION_SCROLL       = "scroll"
ACTION_MOVE         = "move"
ACTION_SCREENSHOT   = "screenshot"
ACTION_DONE         = "done"
ACTION_ASK_USER     = "ask_user"

ALL_ACTION_TYPES = (
    ACTION_CLICK, ACTION_DOUBLE_CLICK, ACTION_TYPE, ACTION_KEY,
    ACTION_SCROLL, ACTION_MOVE, ACTION_SCREENSHOT, ACTION_DONE, ACTION_ASK_USER,
)

# Actions that require explicit confirmation regardless of agent config.
DESTRUCTIVE_ACTION_TYPES: frozenset[str] = frozenset()  # executors may add to this


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseExecutor(ABC):
    """Cross-platform interface for mouse, keyboard, and screenshot operations."""

    @abstractmethod
    def screenshot(self) -> bytes:
        """Capture the full screen and return PNG bytes."""

    @abstractmethod
    def click(self, x: int, y: int, button: str = "left") -> None:
        """Single mouse click at (x, y). button: 'left', 'right', 'middle'."""

    @abstractmethod
    def double_click(self, x: int, y: int) -> None:
        """Double-click at (x, y)."""

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        """Move the mouse to (x, y) without clicking."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type a string of text using keyboard input."""

    @abstractmethod
    def key(self, key_combo: str) -> None:
        """Press a key or key combination. Examples: 'enter', 'ctrl+c', 'alt+f4'."""

    @abstractmethod
    def scroll(
        self,
        x: int,
        y: int,
        direction: str = "down",
        amount: int = 3,
    ) -> None:
        """Scroll at position (x, y). direction: 'up' or 'down'. amount: scroll units."""

    def dispatch(self, action: dict) -> None:
        """Execute an action dict as produced by the agent loop.

        Handles all action types. Raises ValueError for unknown types.
        Does nothing for 'done' and 'ask_user' (handled by the agent loop).
        """
        atype = action.get("type", "")

        if atype in (ACTION_DONE, ACTION_ASK_USER, ACTION_SCREENSHOT):
            return

        if atype == ACTION_CLICK:
            self.click(action["x"], action["y"], action.get("button", "left"))

        elif atype == ACTION_DOUBLE_CLICK:
            self.double_click(action["x"], action["y"])

        elif atype == ACTION_MOVE:
            self.move(action["x"], action["y"])

        elif atype == ACTION_TYPE:
            self.type_text(action["text"])

        elif atype == ACTION_KEY:
            self.key(action["key"])

        elif atype == ACTION_SCROLL:
            self.scroll(
                action.get("x", 0),
                action.get("y", 0),
                action.get("direction", "down"),
                action.get("amount", 3),
            )

        else:
            raise ValueError(f"Unknown action type: {atype!r}")


# ---------------------------------------------------------------------------
# pyautogui helper (shared by all three platform executors)
# ---------------------------------------------------------------------------

def _pyautogui():
    """Return the pyautogui module, raising a clear error if absent."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True   # move mouse to top-left corner to abort
        pyautogui.PAUSE = 0.05      # 50ms inter-action pause for stability
        return pyautogui
    except ImportError as exc:
        raise ImportError(
            "pyautogui is required for desktop control. "
            "Install it with: pip install psr-ai[agent]"
        ) from exc


# ---------------------------------------------------------------------------
# Windows executor
# ---------------------------------------------------------------------------

class WindowsExecutor(BaseExecutor):
    """Desktop executor for Windows using PIL.ImageGrab + pyautogui."""

    def screenshot(self) -> bytes:
        """Capture full screen via PIL.ImageGrab (no extra dependency on Windows)."""
        try:
            from PIL import ImageGrab
            import io
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError as exc:
            raise ImportError(
                "Pillow is required for screenshots on Windows. "
                "Install it with: pip install psr-ai[agent]"
            ) from exc

    def click(self, x: int, y: int, button: str = "left") -> None:
        _pyautogui().click(x, y, button=button)

    def double_click(self, x: int, y: int) -> None:
        _pyautogui().doubleClick(x, y)

    def move(self, x: int, y: int) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)

    def type_text(self, text: str) -> None:
        _pyautogui().write(text, interval=0.02)

    def key(self, key_combo: str) -> None:
        parts = [p.strip() for p in key_combo.lower().split("+")]
        if len(parts) == 1:
            _pyautogui().press(parts[0])
        else:
            _pyautogui().hotkey(*parts)

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)
        clicks = -amount if direction == "down" else amount
        _pyautogui().scroll(clicks)


# ---------------------------------------------------------------------------
# macOS executor
# ---------------------------------------------------------------------------

class MacExecutor(BaseExecutor):
    """Desktop executor for macOS using screencapture (stdlib) + pyautogui."""

    def screenshot(self) -> bytes:
        """Silent screenshot via screencapture -x (same as MacRecorder._capture_step)."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)
        try:
            subprocess.run(
                ["screencapture", "-x", str(tmp)],
                check=False,
                capture_output=True,
            )
            if tmp.exists():
                return tmp.read_bytes()
            return b""
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def click(self, x: int, y: int, button: str = "left") -> None:
        _pyautogui().click(x, y, button=button)

    def double_click(self, x: int, y: int) -> None:
        _pyautogui().doubleClick(x, y)

    def move(self, x: int, y: int) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)

    def type_text(self, text: str) -> None:
        _pyautogui().write(text, interval=0.02)

    def key(self, key_combo: str) -> None:
        parts = [p.strip() for p in key_combo.lower().split("+")]
        if len(parts) == 1:
            _pyautogui().press(parts[0])
        else:
            _pyautogui().hotkey(*parts)

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)
        clicks = -amount if direction == "down" else amount
        _pyautogui().scroll(clicks)


# ---------------------------------------------------------------------------
# Linux executor
# ---------------------------------------------------------------------------

class LinuxExecutor(BaseExecutor):
    """Desktop executor for Linux using native screenshot tools + pyautogui.

    Screenshot tool resolution (same as LinuxRecorder):
      Tier 1: gnome-screenshot
      Tier 2: scrot
      Tier 3: maim

    Mouse/keyboard: pyautogui on X11. xdotool fallback for key combos when
    pyautogui is unavailable or fails on Wayland.

    Note: pyautogui requires an X11 display (DISPLAY env var set). It does not
    work on pure Wayland. If WAYLAND_DISPLAY is set without XWayland, keyboard
    and mouse actions will raise. Set up XWayland or use a VM-based executor.
    """

    _SCREENSHOT_TOOLS = [
        ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
        ("scrot",            ["scrot",            "{path}"]),
        ("maim",             ["maim",             "{path}"]),
    ]

    def __init__(self) -> None:
        import os as _os
        self._screenshot_cmd: list[str] | None = None
        for exe, cmd in self._SCREENSHOT_TOOLS:
            if shutil.which(exe):
                self._screenshot_cmd = cmd
                break
        if _os.environ.get("WAYLAND_DISPLAY"):
            print(
                "Warning: Active Wayland session detected. "
                "PyAutoGUI input injection may be restricted by Linux security profiles. "
                "Switch to an X11 session if mouse or keyboard events fail to trigger."
            )

    def screenshot(self) -> bytes:
        """Capture full screen using the first available Linux screenshot tool."""
        if self._screenshot_cmd is None:
            raise RuntimeError(
                "No screenshot tool found. "
                "Install one: gnome-screenshot, scrot, or maim."
            )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)
        try:
            cmd = [c.format(path=str(tmp)) for c in self._screenshot_cmd]
            subprocess.run(cmd, check=False, capture_output=True)
            if tmp.exists():
                return tmp.read_bytes()
            return b""
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def click(self, x: int, y: int, button: str = "left") -> None:
        _pyautogui().click(x, y, button=button)

    def double_click(self, x: int, y: int) -> None:
        _pyautogui().doubleClick(x, y)

    def move(self, x: int, y: int) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)

    def type_text(self, text: str) -> None:
        _pyautogui().write(text, interval=0.02)

    def key(self, key_combo: str) -> None:
        parts = [p.strip() for p in key_combo.lower().split("+")]
        if len(parts) == 1:
            _pyautogui().press(parts[0])
        else:
            _pyautogui().hotkey(*parts)

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        _pyautogui().moveTo(x, y, duration=0.1)
        clicks = -amount if direction == "down" else amount
        _pyautogui().scroll(clicks)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_executor() -> BaseExecutor:
    """Return the correct executor for the current platform.

    Returns:
      WindowsExecutor on win32
      MacExecutor     on darwin
      LinuxExecutor   on linux*

    Raises RuntimeError on unrecognised platforms.
    """
    platform = sys.platform
    if platform == "win32":
        return WindowsExecutor()
    if platform == "darwin":
        return MacExecutor()
    if platform.startswith("linux"):
        return LinuxExecutor()
    raise RuntimeError(
        f"Unsupported platform: {platform}. "
        "psr-ai agent supports win32, darwin, and linux."
    )
