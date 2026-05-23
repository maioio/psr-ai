"""Controls screen recording: platform factory dispatches to the correct engine.

Windows  : wraps psr.exe via a 5-tier fallback matrix.
macOS    : drives screencapture + osascript via a background click-tracking thread.
Linux    : drives gnome-screenshot / scrot / maim via a background click-tracking thread.

Public surface (unchanged from before):
  Recorder()                       - factory; returns the right engine for sys.platform
  recorder.start_recording(...)    - begin a session
  recorder.stop_recording()        - end a session and return the ZIP / output path
  recorder.is_recording_active()   - True while a session is in progress
  PSRNotFoundError                 - raised on Windows when psr.exe cannot be located
"""

from __future__ import annotations

import importlib.resources
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path

from psr_ai.utils.paths import default_output_dir, resolve_env_path, safe_path, timestamp_filename

# Compiled pattern for sanitizing xprop window-ID output before passing it
# as a subprocess argument. X11 window IDs are always hex strings of the form
# "0x<hex digits>". Anything that does not match is rejected, preventing
# malformed xprop output from injecting unexpected arguments.
_RE_X11_WINDOW_ID = re.compile(r'^0x[0-9a-fA-F]+$')


# ---------------------------------------------------------------------------
# Windows constants and helpers
# ---------------------------------------------------------------------------

_PSR_PROCESS_NAME = "psr.exe"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_ASSET_DIR = _REPO_ROOT / "assets" / "psr_fallback"
_BUNDLED_LNK = _BUNDLED_ASSET_DIR / "psr_shortcut.lnk"
_BUNDLED_EXE_NAME = "psr.exe"

_NOT_FOUND_MESSAGE = """\
PSR.ai could not locate psr.exe on this machine.

Resolution tiers tried (in order):
  1. PATH lookup via shutil.which("psr")
  2. %SystemRoot%\\System32\\psr.exe
  3. %WinDir%\\System32\\psr.exe
  4. Bundled shortcut: assets/psr_fallback/psr_shortcut.lnk
  5. Bundled portable exe: assets/psr_fallback/psr.exe

How to fix this:

Option A - Re-enable PSR (Group Policy):
  Open gpedit.msc and navigate to:
    Computer Configuration > Administrative Templates
      > System > Troubleshooting and Diagnostics
        > Windows Performance PerfTrack
  Set "Enable/Disable PerfTrack" to "Not Configured" or "Enabled".
  Then restart your machine.

Option B - Copy a portable psr.exe manually:
  1. Locate psr.exe on another Windows machine (usually in C:\\Windows\\System32).
  2. Copy it into the PSR.ai asset directory:
       {asset_dir}
  3. Re-run your command.

Option C - Point PSR.ai to any psr.exe via PATH:
  Copy or symlink psr.exe to a directory already on your PATH, then retry.

If psr.exe is blocked by your organisation's security policy, contact your
IT administrator and request an exception for "Microsoft Problem Steps Recorder".
"""


class PSRNotFoundError(RuntimeError):
    """Raised on Windows when psr.exe cannot be located through any resolution tier."""


def _resolve_bundled_asset_dir() -> Path:
    """Return the asset directory using importlib.resources for installed layouts.

    For an editable install the directory is next to the repo root.
    For a wheel install the data_files land under the package data prefix.
    Falls back to the relative path computed from __file__ if both fail.
    """
    try:
        pkg_files = importlib.resources.files("psr_ai")
        candidate = Path(str(pkg_files)).parent.parent / "assets" / "psr_fallback"
        if candidate.is_dir():
            return candidate
    except (TypeError, AttributeError, ModuleNotFoundError):
        pass
    return _BUNDLED_ASSET_DIR


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseRecorder(ABC):
    """Common interface every platform engine must implement."""

    @abstractmethod
    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = 25,
    ) -> Path:
        """Begin a recording session and return the path where output will land."""

    @abstractmethod
    def stop_recording(self) -> Path | None:
        """End the active recording session and return the output path."""

    @abstractmethod
    def is_recording_active(self) -> bool:
        """Return True while a recording session is in progress."""

    # Backward-compatible aliases so existing callers do not need changing.
    def start(self, output_path: str | Path | None = None, max_screenshots: int = 25) -> Path:
        return self.start_recording(output_path=output_path, max_screenshots=max_screenshots)

    def stop(self) -> Path | None:
        return self.stop_recording()


# ---------------------------------------------------------------------------
# Windows engine
# ---------------------------------------------------------------------------

class WindowsRecorder(BaseRecorder):
    """Wraps psr.exe with a 5-tier fallback matrix and UAC elevation handling.

    Resolution order for psr.exe (first match wins):
      Fast path : shutil.which("psr")
      Tier 1    : %SystemRoot%\\System32\\psr.exe
      Tier 2    : %WinDir%\\System32\\psr.exe
      Tier 3a   : assets/psr_fallback/psr.exe (portable bundled binary)
      Tier 3b   : assets/psr_fallback/psr_shortcut.lnk (Shell shortcut)
    """

    _STATE_FILE = Path.home() / "psr_output" / ".current_session"

    def __init__(self) -> None:
        self._psr_exe: Path = self._resolve_psr()
        self._pending_output: Path | None = self._load_state()

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_psr(self) -> Path:
        """Return the path to use when launching PSR, trying all tiers in order."""
        found = shutil.which("psr")
        if found:
            return Path(found)

        try:
            tier1 = resolve_env_path("SystemRoot", "System32", "psr.exe")
            if tier1.exists():
                return tier1
        except KeyError:
            pass

        try:
            tier2 = resolve_env_path("WinDir", "System32", "psr.exe")
            if tier2.exists():
                return tier2
        except KeyError:
            pass

        asset_dir = _resolve_bundled_asset_dir()

        bundled_exe = asset_dir / _BUNDLED_EXE_NAME
        if bundled_exe.exists():
            return bundled_exe

        bundled_lnk = asset_dir / "psr_shortcut.lnk"
        if bundled_lnk.exists():
            return bundled_lnk

        raise PSRNotFoundError(
            _NOT_FOUND_MESSAGE.format(asset_dir=str(asset_dir))
        )

    @property
    def _use_shell(self) -> bool:
        """True when launching via a .lnk shortcut - Windows Shell must resolve it."""
        return self._psr_exe.suffix.lower() == ".lnk"

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = 25,
    ) -> Path:
        """Launch psr.exe in the background and return the ZIP output path.

        Falls back to ShellExecuteW automatically on WinError 740 (UAC elevation
        required) so the user never needs to run the terminal as Administrator.
        """
        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".zip")

        out = safe_path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self._pending_output = out
        self._save_state(out)

        args = [
            str(self._psr_exe),
            "/start",
            "/output", str(out),
            "/sc", "1",
            "/maxsc", str(max_screenshots),
            "/gui", "0",
        ]
        self._launch(args)
        return out

    def stop_recording(self) -> Path | None:
        """Send the /stop signal to psr.exe and wait for it to commit the ZIP."""
        self._launch([str(self._psr_exe), "/stop"])
        out = self._pending_output or self._find_latest_zip()
        self._clear_state()
        return out

    @classmethod
    def _load_state(cls) -> Path | None:
        try:
            p = Path(cls._STATE_FILE.read_text(encoding="utf-8").strip())
            return p if p.exists() else None
        except (OSError, ValueError):
            return None

    @classmethod
    def _save_state(cls, path: Path) -> None:
        try:
            cls._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cls._STATE_FILE.write_text(str(path), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def _clear_state(cls) -> None:
        try:
            cls._STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _find_latest_zip() -> Path | None:
        out_dir = Path.home() / "psr_output"
        zips = sorted(out_dir.glob("psr_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return zips[0] if zips else None

    def is_recording_active(self) -> bool:
        """Return True if a psr.exe process is currently running on this machine."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {_PSR_PROCESS_NAME}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return _PSR_PROCESS_NAME.lower() in result.stdout.lower()
        except FileNotFoundError:
            return False

    def _launch(self, args: list[str]) -> None:
        """Launch a psr.exe command; fall back to ShellExecuteW on WinError 740.

        WinError 740 means the executable has a UAC elevation manifest.
        ShellExecuteW honours that manifest and handles the elevation prompt
        transparently without requiring the user to run the terminal as Admin.
        """
        try:
            subprocess.Popen(
                args,
                shell=self._use_shell,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                ),
            )
        except OSError as exc:
            if sys.platform == "win32" and getattr(exc, "winerror", None) == 740:
                self._shell_execute(args)
            else:
                raise

    @staticmethod
    def _shell_execute(args: list[str]) -> None:
        """Launch a command via Windows ShellExecuteW.

        Bypasses the need for the calling process to already be elevated.
        Windows handles the UAC manifest of the target executable directly.
        args[0] is the executable; args[1:] are joined as a parameter string.
        """
        import ctypes
        exe = args[0]
        params = " ".join(
            f'"{a}"' if " " in a else a
            for a in args[1:]
        )
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "open",
            exe,
            params,
            None,
            0,
        )
        if ret <= 32:
            raise OSError(
                f"ShellExecuteW failed with code {ret} for: {exe} {params}"
            )


# ---------------------------------------------------------------------------
# Shared ZIP packaging helper (Mac and Linux)
# ---------------------------------------------------------------------------

def _pack_to_zip(asset_dir: Path, zip_path: Path) -> Path:
    """Pack data.json and all screenshots from asset_dir into a ZIP at zip_path.

    Layout inside the ZIP mirrors the structure parse_session expects:
      data.json
      screenshots/step_000.png
      screenshots/step_001.png
      ...

    The asset_dir is removed after packing to avoid leaving duplicate data on
    disk. Returns zip_path.

    Raises OSError if zip_path cannot be written.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    screenshots_dir = asset_dir / "screenshots"
    data_json = asset_dir / "data.json"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if data_json.exists():
            zf.write(data_json, arcname="data.json")

        if screenshots_dir.exists():
            for png in sorted(screenshots_dir.iterdir()):
                if png.suffix.lower() == ".png":
                    zf.write(png, arcname=f"screenshots/{png.name}")

    # Remove the temporary asset directory now that everything is in the ZIP.
    try:
        shutil.rmtree(asset_dir)
    except OSError:
        pass  # Non-fatal: the ZIP was written successfully.

    return zip_path


# ---------------------------------------------------------------------------
# macOS engine
# ---------------------------------------------------------------------------

class MacRecorder(BaseRecorder):
    """Records screen sessions on macOS using screencapture + osascript.

    A background thread listens for mouse clicks via pynput (optional extra
    [desktop]). On each click it captures the frontmost application name via
    osascript and fires screencapture -x to take a silent screenshot.

    When pynput is not installed the recorder falls back to a 3-second
    interval capture with idle-frame deduplication: frames where both the
    frontmost application name and the pointer coordinates are identical to
    the previous step are discarded to prevent disk bloat.

    stop_recording() packs all assets into a standard ZIP so downstream
    components (parse_session, sa pub, sa analyze) receive the same format
    as Windows PSR output.
    """

    _INTERVAL_SECONDS = 3

    def __init__(self) -> None:
        self._output_dir: Path | None = None
        self._active = False
        self._step_index = 0
        self._steps: list[dict] = []
        self._thread: threading.Thread | None = None
        # Tracks the last captured program name and coordinates for dedup.
        self._last_program: str = ""
        self._last_x: int = -1
        self._last_y: int = -1

    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = 25,
    ) -> Path:
        """Start background capture thread and return the staging directory path."""
        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".d")

        self._output_dir = Path(output_path)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        (self._output_dir / "screenshots").mkdir(exist_ok=True)

        self._active = True
        self._step_index = 0
        self._steps = []
        self._last_program = ""
        self._last_x = -1
        self._last_y = -1
        self._max = max_screenshots

        try:
            from pynput import mouse as _mouse
            listener = _mouse.Listener(on_click=self._on_click)
            self._thread = threading.Thread(target=listener.start, daemon=True)
        except ImportError:
            self._thread = threading.Thread(
                target=self._interval_capture,
                daemon=True,
            )

        self._thread.start()
        return self._output_dir

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        """pynput callback: capture a screenshot on mouse-button-down."""
        if not pressed or not self._active:
            return
        if self._step_index >= self._max:
            return
        self._capture_step(click_x=x, click_y=y)

    def _interval_capture(self) -> None:
        """Fallback: take a screenshot every _INTERVAL_SECONDS.

        Idle frames (same frontmost app and same pointer position as the
        previous step) are discarded before any disk I/O occurs.
        """
        while self._active and self._step_index < self._max:
            program = self._frontmost_app()
            # Coordinates are unavailable without pynput; use sentinel (0, 0).
            x, y = 0, 0
            if self._is_idle(program, x, y):
                time.sleep(self._INTERVAL_SECONDS)
                continue
            self._capture_step(click_x=x, click_y=y, program_hint=program)
            time.sleep(self._INTERVAL_SECONDS)

    def _is_idle(self, program: str, x: int, y: int) -> bool:
        """Return True when the current state is identical to the last captured step."""
        return (
            program == self._last_program
            and x == self._last_x
            and y == self._last_y
        )

    def _frontmost_app(self) -> str:
        """Return the name of the frontmost macOS application, or empty string."""
        try:
            result = subprocess.run(
                [
                    "osascript", "-e",
                    'tell application "System Events" to get name of first process '
                    'whose frontmost is true',
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip()
        except FileNotFoundError:
            return ""

    def _capture_step(
        self,
        click_x: int = 0,
        click_y: int = 0,
        program_hint: str | None = None,
    ) -> None:
        """Take one silent screenshot and record the frontmost app name."""
        if self._output_dir is None:
            return
        idx = self._step_index
        self._step_index += 1

        png_path = self._output_dir / "screenshots" / f"step_{idx:03d}.png"
        ts = time.strftime("%m/%d/%Y %I:%M:%S %p")

        try:
            subprocess.run(
                ["screencapture", "-x", str(png_path)],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass

        program = program_hint if program_hint is not None else self._frontmost_app()

        # Update dedup state after a successful capture.
        self._last_program = program
        self._last_x = click_x
        self._last_y = click_y

        self._steps.append({
            "index": idx,
            "label": f"Step {idx}",
            "timestamp": ts,
            "ui_element": "",
            "program": program,
            "action_text": f"Click at ({click_x}, {click_y})" if click_x or click_y else "Screenshot",
            "screenshot": f"screenshots/step_{idx:03d}.png",
            "click_x": click_x,
            "click_y": click_y,
        })

    def stop_recording(self) -> Path | None:
        """Stop the capture thread, pack assets into a ZIP, and return the ZIP path.

        The ZIP is written next to the staging directory and the staging
        directory is removed after packing. Returns None if recording was
        never started.
        """
        self._active = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

        if self._output_dir is None:
            return None

        self._write_data_json()

        # Pack the staging directory into a ZIP with the same stem name.
        zip_path = self._output_dir.with_suffix(".zip")
        return _pack_to_zip(self._output_dir, zip_path)

    def _write_data_json(self) -> None:
        """Serialise the in-memory step list to data.json inside the staging dir."""
        payload = {
            "title": "macOS Recording",
            "source": (self._output_dir.stem if self._output_dir else "unknown") + ".zip",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step_count": len(self._steps),
            "steps": self._steps,
        }
        out = self._output_dir / "data.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def is_recording_active(self) -> bool:
        """Return True while the capture thread is running."""
        return self._active and (self._thread is not None) and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Linux engine
# ---------------------------------------------------------------------------

class LinuxRecorder(BaseRecorder):
    """Records screen sessions on Linux using native CLI screenshot tools.

    Screenshot tool resolution (first found on PATH wins):
      Tier 1: gnome-screenshot -f <PATH>
      Tier 2: scrot <PATH>
      Tier 3: maim <PATH>

    Active window detection: when WAYLAND_DISPLAY is set, gdbus is tried
    first (native Wayland apps). xprop (X11) is the fallback. When
    WAYLAND_DISPLAY is absent, xprop is tried first, gdbus second.

    Like MacRecorder, a pynput-based click listener is used when the
    [desktop] extra is installed; otherwise falls back to 3-second interval
    capture with idle-frame deduplication.

    stop_recording() packs all assets into a standard ZIP for uniform
    downstream ingestion.
    """

    _INTERVAL_SECONDS = 3

    # Screenshot tool commands keyed by executable name.
    _SCREENSHOT_TOOLS: list[tuple[str, list[str]]] = [
        ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
        ("scrot",            ["scrot",            "{path}"]),
        ("maim",             ["maim",             "{path}"]),
    ]

    def __init__(self) -> None:
        self._output_dir: Path | None = None
        self._active = False
        self._step_index = 0
        self._steps: list[dict] = []
        self._thread: threading.Thread | None = None
        self._screenshot_cmd: list[str] | None = self._resolve_screenshot_tool()
        # Dedup state for interval-capture fallback.
        self._last_program: str = ""
        self._last_x: int = -1
        self._last_y: int = -1

    def _resolve_screenshot_tool(self) -> list[str] | None:
        """Return the first available screenshot command template, or None."""
        for exe, cmd in self._SCREENSHOT_TOOLS:
            if shutil.which(exe):
                return cmd
        return None

    def _active_window_name(self) -> str:
        """Return the name of the currently focused window, or empty string.

        Detection order depends on the runtime display server:
        - If WAYLAND_DISPLAY is set: gdbus is tried first (native Wayland apps
          on GNOME are not visible to xprop). Falls back to xprop for
          XWayland-hosted windows if gdbus returns empty.
        - If WAYLAND_DISPLAY is absent (pure X11): xprop is tried first,
          gdbus second.
        """
        on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))

        if on_wayland:
            name = self._try_gdbus()
            if name:
                return name
            return self._try_xprop()

        name = self._try_xprop()
        if name:
            return name
        return self._try_gdbus()

    def _try_gdbus(self) -> str:
        """Query the active window title via gdbus (Wayland/GNOME Shell)."""
        if not shutil.which("gdbus"):
            return ""
        try:
            result = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.gnome.Shell",
                    "--object-path", "/org/gnome/Shell",
                    "--method", "org.gnome.Shell.Eval",
                    "global.display.focus_window?.title ?? ''",
                ],
                capture_output=True, text=True, check=False,
            )
            # Output: ('(true, "Window Title")',)
            raw = result.stdout.strip()
            if raw:
                m = re.search(r'"([^"]*)"', raw)
                if m:
                    return m.group(1)
        except (FileNotFoundError, OSError):
            pass
        return ""

    def _try_xprop(self) -> str:
        """Query the active window title via xprop (X11)."""
        if not shutil.which("xprop"):
            return ""
        try:
            root_result = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, check=False,
            )
            parts = root_result.stdout.split()
            if not parts:
                return ""
            raw_id = parts[-1]
            # Validate: must be a hex window ID of the form "0x<digits>".
            if not _RE_X11_WINDOW_ID.match(raw_id):
                return ""
            result = subprocess.run(
                ["xprop", "-id", raw_id, "_NET_WM_NAME"],
                capture_output=True, text=True, check=False,
            )
            # Output format: _NET_WM_NAME(UTF8_STRING) = "Window Title"
            if "=" in result.stdout:
                return result.stdout.split("=", 1)[1].strip().strip('"')
        except (IndexError, ValueError, FileNotFoundError, OSError):
            pass
        return ""

    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = 25,
    ) -> Path:
        """Start background capture thread and return the staging directory path."""
        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".d")

        self._output_dir = Path(output_path)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        (self._output_dir / "screenshots").mkdir(exist_ok=True)

        self._active = True
        self._step_index = 0
        self._steps = []
        self._last_program = ""
        self._last_x = -1
        self._last_y = -1
        self._max = max_screenshots

        try:
            from pynput import mouse as _mouse
            listener = _mouse.Listener(on_click=self._on_click)
            self._thread = threading.Thread(target=listener.start, daemon=True)
        except ImportError:
            self._thread = threading.Thread(
                target=self._interval_capture,
                daemon=True,
            )

        self._thread.start()
        return self._output_dir

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        """pynput callback: capture a screenshot on mouse-button-down."""
        if not pressed or not self._active:
            return
        if self._step_index >= self._max:
            return
        self._capture_step(click_x=x, click_y=y)

    def _interval_capture(self) -> None:
        """Fallback: take a screenshot every _INTERVAL_SECONDS.

        Idle frames (same frontmost window and same pointer position as the
        previous step) are discarded before any disk I/O occurs.
        """
        while self._active and self._step_index < self._max:
            program = self._active_window_name()
            x, y = 0, 0
            if self._is_idle(program, x, y):
                time.sleep(self._INTERVAL_SECONDS)
                continue
            self._capture_step(click_x=x, click_y=y, program_hint=program)
            time.sleep(self._INTERVAL_SECONDS)

    def _is_idle(self, program: str, x: int, y: int) -> bool:
        """Return True when the current state is identical to the last captured step."""
        return (
            program == self._last_program
            and x == self._last_x
            and y == self._last_y
        )

    def _capture_step(
        self,
        click_x: int = 0,
        click_y: int = 0,
        program_hint: str | None = None,
    ) -> None:
        """Take one screenshot using the resolved CLI tool."""
        if self._output_dir is None:
            return
        idx = self._step_index
        self._step_index += 1

        png_path = self._output_dir / "screenshots" / f"step_{idx:03d}.png"
        ts = time.strftime("%m/%d/%Y %I:%M:%S %p")
        program = program_hint if program_hint is not None else self._active_window_name()

        if self._screenshot_cmd is not None:
            cmd = [c.format(path=str(png_path)) for c in self._screenshot_cmd]
            try:
                subprocess.run(cmd, check=False, capture_output=True)
            except FileNotFoundError:
                pass

        # Update dedup state after capture.
        self._last_program = program
        self._last_x = click_x
        self._last_y = click_y

        self._steps.append({
            "index": idx,
            "label": f"Step {idx}",
            "timestamp": ts,
            "ui_element": "",
            "program": program,
            "action_text": f"Click at ({click_x}, {click_y})" if click_x or click_y else "Screenshot",
            "screenshot": f"screenshots/step_{idx:03d}.png",
            "click_x": click_x,
            "click_y": click_y,
        })

    def stop_recording(self) -> Path | None:
        """Stop the capture thread, pack assets into a ZIP, and return the ZIP path.

        Returns None if recording was never started.
        """
        self._active = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

        if self._output_dir is None:
            return None

        self._write_data_json()

        zip_path = self._output_dir.with_suffix(".zip")
        return _pack_to_zip(self._output_dir, zip_path)

    def _write_data_json(self) -> None:
        """Serialise the in-memory step list to data.json inside the staging dir."""
        payload = {
            "title": "Linux Recording",
            "source": (self._output_dir.stem if self._output_dir else "unknown") + ".zip",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step_count": len(self._steps),
            "steps": self._steps,
        }
        out = self._output_dir / "data.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def is_recording_active(self) -> bool:
        """Return True while the capture thread is running."""
        return self._active and (self._thread is not None) and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def Recorder() -> BaseRecorder:
    """Return the correct recorder engine for the current platform.

    Returns:
      WindowsRecorder on win32
      MacRecorder     on darwin
      LinuxRecorder   on linux

    Raises PSRNotFoundError on Windows when psr.exe cannot be located.
    Raises RuntimeError on any other unrecognised platform.
    """
    platform = sys.platform
    if platform == "win32":
        return WindowsRecorder()
    if platform == "darwin":
        return MacRecorder()
    if platform.startswith("linux"):
        return LinuxRecorder()
    raise RuntimeError(
        f"Unsupported platform: {platform}. "
        "PSR.ai supports win32, darwin, and linux."
    )
