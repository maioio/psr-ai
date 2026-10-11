"""Controls screen recording: platform factory dispatches to the correct engine.

Windows  : wraps psr.exe via a 5-tier fallback matrix. When psr.exe is missing,
           blocked by Group Policy, or raises PermissionError, the engine falls
           back silently to a detached local polling daemon.
macOS    : drives screencapture + osascript via a background click-tracking thread.
Linux    : drives gnome-screenshot / scrot / maim via a background click-tracking thread.

Public surface (unchanged from before):
  Recorder()                       - factory; returns the right engine for sys.platform
  recorder.start_recording(...)    - begin a session
  recorder.stop_recording()        - end a session and return the ZIP / output path
  recorder.is_recording_active()   - True while a session is in progress
  PSRNotFoundError                 - raised on Windows when psr.exe cannot be located

Added in 0.2.0:
  run_polling_session(...)         - the polling loop body, executed by the
                                     detached daemon (`sa record-daemon`)
  _DEFAULT_MAX_SCREENSHOTS         - memory guard, 100 frames, enforced by every
                                     capture path (click-driven and interval)
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
import tempfile
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path

from psr_ai.utils.paths import default_output_dir, resolve_env_path, safe_path, timestamp_filename

# Memory guard: hard cap on frames per session across every engine. Reaching it
# ends the session cleanly (data.json written, ZIP packed, daemon exits) instead
# of letting a forgotten recording fill the disk.
_DEFAULT_MAX_SCREENSHOTS = 100

# Seconds between frames in every polling engine.
_POLL_INTERVAL_SECONDS = 3.0

# Sentinel file dropped into the staging directory by stop_recording(). The
# polling daemon polls for it, which is how a *different* process (`sa stop`)
# ends a session started by `sa start`.
_STOP_SENTINEL = ".stop"

# How long stop_recording() waits for the daemon to commit its ZIP.
_STOP_TIMEOUT_SECONDS = 30.0

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
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
    ) -> Path:
        """Begin a recording session and return the path where output will land."""

    @abstractmethod
    def stop_recording(self) -> Path | None:
        """End the active recording session and return the output path."""

    @abstractmethod
    def is_recording_active(self) -> bool:
        """Return True while a recording session is in progress."""

    # Backward-compatible aliases so existing callers do not need changing.
    def start(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
    ) -> Path:
        return self.start_recording(
            output_path=output_path, max_screenshots=max_screenshots, max_dim=max_dim
        )

    def stop(self) -> Path | None:
        return self.stop_recording()


# ---------------------------------------------------------------------------
# Frame de-duplication helper (Pillow-based, zero new dependencies)
# ---------------------------------------------------------------------------

# 8x8 grayscale thumbnail comparison threshold. Two frames are considered
# "identical" when the sum of per-pixel brightness differences across the
# 64-pixel thumbnail is below this value. This tolerates minor noise (e.g.
# a blinking cursor or clock tick) without flagging a real UI change.
_DEDUP_THUMB_SIZE = (8, 8)
_DEDUP_DIFF_THRESHOLD = 12


def _image_signature(img) -> tuple[int, ...]:
    """Return an 8x8 grayscale pixel signature for a live PIL image.

    Comparing signatures *before* the frame reaches disk is what makes the
    polling loop write zero bytes for an idle screen.
    """
    from PIL import Image  # Local import: Pillow is an optional extra.

    thumb = img.convert("L").resize(_DEDUP_THUMB_SIZE, Image.BILINEAR)
    # tobytes() over getdata(): one byte per pixel in "L" mode, no deprecation
    # (getdata is removed in Pillow 14), and no per-pixel Python objects.
    return tuple(thumb.tobytes())


def _frame_thumb_signature(png_path: Path) -> tuple[int, ...] | None:
    """Return an 8x8 grayscale pixel signature for a PNG already on disk.

    Returns None if Pillow is unavailable or the image cannot be read -
    callers should treat that as "always save" (dedup disabled).
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(png_path) as img:
            return _image_signature(img)
    except OSError:
        return None


def _frames_are_similar(
    sig_a: tuple[int, ...] | None, sig_b: tuple[int, ...] | None
) -> bool:
    """Return True when two thumbnail signatures represent the same screen."""
    if sig_a is None or sig_b is None:
        return False
    diff = sum(abs(a - b) for a, b in zip(sig_a, sig_b))
    return diff < _DEDUP_DIFF_THRESHOLD


def _downscale_to_max_dim(png_path: Path, max_dim: int) -> None:
    """Downscale a screenshot in-place so its longest edge is <= max_dim.

    Used by --low-token to cut LLM vision token costs (Claude/GPT-4o charge
    per 512x512 tile). No-op if Pillow is unavailable or the image is
    already small enough.
    """
    try:
        from PIL import Image
    except ImportError:
        return

    try:
        with Image.open(png_path) as img:
            if max(img.width, img.height) <= max_dim:
                return
            ratio = max_dim / max(img.width, img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
            resized.save(png_path)
    except OSError:
        pass


def _downscale_image(img, max_dim: int):
    """Return img scaled so its longest edge is <= max_dim (in memory)."""
    from PIL import Image

    longest = max(img.width, img.height)
    if longest <= max_dim:
        return img
    ratio = max_dim / longest
    return img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Native screenshot backends (no third-party capture dependency required)
# ---------------------------------------------------------------------------

# PowerShell one-liner using System.Drawing - the native Windows screen-copy
# path. Used when Pillow's ImageGrab is unavailable, which is exactly the
# environment where psr.exe tends to be locked down too.
_PS_CAPTURE = (
    "Add-Type -AssemblyName System.Drawing,System.Windows.Forms; "
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); "
    "$g=[System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
    "$bmp.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png); "
    "$g.Dispose(); $bmp.Dispose()"
)

# Linux CLI screenshot tools, in resolution order.
_LINUX_SCREENSHOT_TOOLS: list[tuple[str, list[str]]] = [
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
    ("scrot",            ["scrot",            "{path}"]),
    ("maim",             ["maim",             "{path}"]),
]


def _native_capture_to_file(png_path: Path) -> bool:
    """Capture the screen to png_path using the OS's own screenshot tooling.

    Windows : powershell + System.Drawing.Graphics.CopyFromScreen
    macOS   : screencapture -x
    Linux   : gnome-screenshot / scrot / maim

    Returns True when a file was produced. Never raises.
    """
    if sys.platform == "win32":
        cmd = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            _PS_CAPTURE.format(path=str(png_path).replace("'", "''")),
        ]
    elif sys.platform == "darwin":
        cmd = ["screencapture", "-x", str(png_path)]
    else:
        cmd = []
        for exe, template in _LINUX_SCREENSHOT_TOOLS:
            if shutil.which(exe):
                cmd = [part.format(path=str(png_path)) for part in template]
                break
        if not cmd:
            return False

    try:
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
    except (OSError, ValueError):
        return False
    return png_path.exists() and png_path.stat().st_size > 0


def _grab_screen_image():
    """Return a PIL image of the current screen, or None when unavailable.

    Fast path is Pillow's ImageGrab (itself a thin wrapper over the native
    Win32/Quartz capture APIs). When that is missing or fails, the frame is
    taken with the OS CLI tool and loaded back from a temp file.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    if sys.platform in ("win32", "darwin"):
        try:
            from PIL import ImageGrab
            return ImageGrab.grab()
        except Exception:
            pass  # Fall through to the CLI backend.

    tmp_dir = Path(tempfile.gettempdir())
    tmp_png = tmp_dir / f"psr_frame_{os.getpid()}_{int(time.time() * 1000)}.png"
    try:
        if not _native_capture_to_file(tmp_png):
            return None
        with Image.open(tmp_png) as img:
            return img.copy()  # Detach from the file so it can be deleted.
    except OSError:
        return None
    finally:
        try:
            tmp_png.unlink(missing_ok=True)
        except OSError:
            pass


def _foreground_window_title() -> str:
    """Return the title of the active window, or empty string.

    Windows uses the Win32 API directly; other platforms reuse the engine
    helpers (osascript / xprop / gdbus) via the polling session.
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Polling session - the body of the detached recording daemon
# ---------------------------------------------------------------------------

class _PollingSession:
    """Runs one full polling recording, start to packed ZIP, in this process.

    The session ends - cleanly, on its own - as soon as either condition holds:
      * the frame count reaches max_screenshots (memory guard), or
      * the stop sentinel appears in the staging directory (`sa stop`).

    Every frame is hashed in memory before it is written, so an idle screen
    produces no file at all rather than a file that is written then deleted.
    """

    def __init__(
        self,
        staging_dir: Path,
        zip_path: Path,
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        interval: float = _POLL_INTERVAL_SECONDS,
        max_dim: int | None = None,
        title: str = "Polling Recording (fallback mode)",
    ) -> None:
        self._staging = Path(staging_dir)
        self._zip_path = Path(zip_path)
        self._max = max(1, int(max_screenshots))
        self._interval = max(0.2, float(interval))
        self._max_dim = max_dim
        self._title = title
        self._steps: list[dict] = []
        self._count = 0
        self._last_sig: tuple[int, ...] | None = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def run(self) -> Path:
        """Capture until the cap or the stop sentinel, then pack and return the ZIP.

        The interval loop always runs, and clicks (when pynput is installed)
        add captures on top of it. Making clicks the *only* trigger would mean
        an unattended session records nothing at all; because idle frames are
        discarded before they are written, the interval loop costs nothing when
        the screen is not changing.
        """
        (self._staging / "screenshots").mkdir(parents=True, exist_ok=True)

        listener = self._start_click_listener()
        try:
            while not self.should_stop():
                self.capture()
                if self.should_stop():
                    break
                time.sleep(self._interval)
        finally:
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass

        return self.finalize()

    def should_stop(self) -> bool:
        """True when the memory guard is hit or `sa stop` dropped the sentinel."""
        if self._count >= self._max:
            return True
        return (self._staging / _STOP_SENTINEL).exists()

    def _start_click_listener(self):
        """Return a started pynput listener, or None when the extra is absent."""
        try:
            from pynput import mouse as _mouse
        except ImportError:
            return None
        try:
            listener = _mouse.Listener(on_click=self._on_click)
            listener.start()
            return listener
        except Exception:
            return None

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed or self.should_stop():
            return
        self.capture(click_x=x, click_y=y)

    # -- capture --------------------------------------------------------

    def capture(self, click_x: int = 0, click_y: int = 0) -> bool:
        """Take one frame. Returns True when a frame was actually saved."""
        with self._lock:
            if self._count >= self._max:
                return False
            idx = self._count
            png_path = self._staging / "screenshots" / f"step_{idx:03d}.png"
            try:
                # Cheap on an existing directory, and keeps a session alive if
                # the staging folder is removed underneath us mid-recording.
                png_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
            program = _foreground_window_title()
            ts = time.strftime("%m/%d/%Y %I:%M:%S %p")

            img = _grab_screen_image()
            if img is None:
                # No Pillow at all: capture straight to disk, without diffing.
                if not _native_capture_to_file(png_path):
                    return False
            else:
                try:
                    signature = _image_signature(img)
                    if _frames_are_similar(signature, self._last_sig):
                        return False  # Idle frame - nothing touches the disk.
                    self._last_sig = signature
                    frame = img if self._max_dim is None else _downscale_image(img, self._max_dim)
                    frame.convert("RGB").save(png_path)
                finally:
                    try:
                        img.close()
                    except Exception:
                        pass

            self._steps.append({
                "index": idx,
                "label": f"Step {idx}",
                "timestamp": ts,
                "ui_element": "",
                "program": program,
                "action_text": (
                    f"Click at ({click_x}, {click_y})" if click_x or click_y else "Screenshot"
                ),
                "screenshot": f"screenshots/step_{idx:03d}.png",
                "click_x": click_x,
                "click_y": click_y,
            })
            self._count += 1
            return True

    # -- teardown -------------------------------------------------------

    def finalize(self) -> Path:
        """Write data.json, pack the ZIP, and remove the staging directory."""
        payload = {
            "title": self._title,
            "source": self._zip_path.name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step_count": len(self._steps),
            "steps": self._steps,
        }
        try:
            (self._staging / "data.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
        try:
            (self._staging / _STOP_SENTINEL).unlink(missing_ok=True)
        except OSError:
            pass
        return _pack_to_zip(self._staging, self._zip_path)


def run_polling_session(
    staging_dir: str | Path,
    zip_path: str | Path,
    max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
    interval: float = _POLL_INTERVAL_SECONDS,
    max_dim: int | None = None,
) -> Path:
    """Run a complete polling recording in the calling process.

    This is the entry point of the detached daemon spawned by
    WindowsPollingRecorder.start_recording() (`sa record-daemon`). It returns
    only once the session has ended and the ZIP has been committed.
    """
    session = _PollingSession(
        staging_dir=Path(staging_dir),
        zip_path=Path(zip_path),
        max_screenshots=max_screenshots,
        interval=interval,
        max_dim=max_dim,
    )
    return session.run()


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
        # Set when psr.exe turns out to be unusable at launch time (Group
        # Policy block, PermissionError). From then on every call is delegated.
        self._fallback: WindowsPollingRecorder | None = None

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
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
    ) -> Path:
        """Launch psr.exe in the background and return the ZIP output path.

        Falls back to ShellExecuteW automatically on WinError 740 (UAC elevation
        required) so the user never needs to run the terminal as Administrator.

        If psr.exe still cannot be launched - Group Policy block (WinError 1260),
        PermissionError, or any other OSError - the call is handed to the local
        polling daemon silently. The caller sees an ordinary ZIP path either way.

        max_dim only applies to the polling fallback; psr.exe controls its own
        capture resolution and PSR.ai does not post-process its screenshots.
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
        try:
            self._launch(args)
        except OSError:
            # PermissionError, ERROR_ACCESS_DISABLED_BY_POLICY (1260), a missing
            # binary, a failed ShellExecuteW - all mean the same thing here:
            # psr.exe is not usable on this machine. Degrade, do not fail.
            self._clear_state()
            self._fallback = WindowsPollingRecorder()
            return self._fallback.start_recording(
                output_path=out,
                max_screenshots=max_screenshots,
                max_dim=max_dim,
            )
        return out

    def stop_recording(self) -> Path | None:
        """Send the /stop signal to psr.exe and wait for it to commit the ZIP."""
        # getattr keeps recorders built via object.__new__ (tests, pickling) working.
        fallback = getattr(self, "_fallback", None)
        if fallback is not None:
            return fallback.stop_recording()

        try:
            self._launch([str(self._psr_exe), "/stop"])
        except OSError:
            # The session was never really running under psr.exe; if a polling
            # session is live, end that instead.
            if WindowsPollingRecorder.has_active_session():
                self._clear_state()
                return WindowsPollingRecorder().stop_recording()
            raise
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
        fallback = getattr(self, "_fallback", None)
        if fallback is not None:
            return fallback.is_recording_active()
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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

    # Build into a temp file and swap it in. zipfile creates the target the
    # moment it opens for writing, so packing in place would make an empty,
    # unreadable ZIP visible to a concurrent `sa stop` polling for it.
    tmp_zip = zip_path.with_name(zip_path.name + ".part")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        if data_json.exists():
            zf.write(data_json, arcname="data.json")

        if screenshots_dir.exists():
            for png in sorted(screenshots_dir.iterdir()):
                if png.suffix.lower() == ".png":
                    zf.write(png, arcname=f"screenshots/{png.name}")
    os.replace(tmp_zip, zip_path)

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

    _INTERVAL_SECONDS = _POLL_INTERVAL_SECONDS

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
        # Tracks the last captured frame's pixel signature for visual dedup.
        self._last_thumb: tuple[int, ...] | None = None
        self._max_dim: int | None = None
        # Set when the memory guard ends the session before stop_recording().
        self._finalized_zip: Path | None = None

    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
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
        self._last_thumb = None
        self._max_dim = max_dim
        self._max = max_screenshots
        self._finalized_zip = None

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
            self._autostop()
            return
        self._capture_step(click_x=x, click_y=y)
        if self._step_index >= self._max:
            self._autostop()

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

        if self._step_index >= self._max:
            self._autostop()

    def _autostop(self) -> None:
        """Memory guard: the frame cap was reached - end the session cleanly.

        Writes data.json, packs the ZIP, and marks the session finished so the
        capture thread stops and a later stop_recording() is a no-op returning
        the same path.
        """
        if self._finalized_zip is not None:
            return
        self._active = False
        if self._output_dir is None:
            return
        self._write_data_json()
        self._finalized_zip = _pack_to_zip(
            self._output_dir, self._output_dir.with_suffix(".zip")
        )

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

        # Visual dedup: discard if the screen is pixel-identical to the last frame.
        if png_path.exists():
            thumb = _frame_thumb_signature(png_path)
            if _frames_are_similar(thumb, self._last_thumb):
                png_path.unlink(missing_ok=True)
                self._step_index -= 1
                return
            self._last_thumb = thumb
            if self._max_dim:
                _downscale_to_max_dim(png_path, self._max_dim)

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

        # The memory guard may have packed the ZIP already; never pack twice.
        if getattr(self, "_finalized_zip", None) is not None:
            return self._finalized_zip

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

    _INTERVAL_SECONDS = _POLL_INTERVAL_SECONDS

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
        # Tracks the last captured frame's pixel signature for visual dedup.
        self._last_thumb: tuple[int, ...] | None = None
        self._max_dim: int | None = None
        # Set when the memory guard ends the session before stop_recording().
        self._finalized_zip: Path | None = None

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
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
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
        self._last_thumb = None
        self._max_dim = max_dim
        self._max = max_screenshots
        self._finalized_zip = None

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
            self._autostop()
            return
        self._capture_step(click_x=x, click_y=y)
        if self._step_index >= self._max:
            self._autostop()

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

        if self._step_index >= self._max:
            self._autostop()

    def _autostop(self) -> None:
        """Memory guard: the frame cap was reached - end the session cleanly.

        Writes data.json, packs the ZIP, and marks the session finished so the
        capture thread stops and a later stop_recording() is a no-op returning
        the same path.
        """
        if self._finalized_zip is not None:
            return
        self._active = False
        if self._output_dir is None:
            return
        self._write_data_json()
        self._finalized_zip = _pack_to_zip(
            self._output_dir, self._output_dir.with_suffix(".zip")
        )

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

        # Visual dedup: discard if the screen is pixel-identical to the last frame.
        if png_path.exists():
            thumb = _frame_thumb_signature(png_path)
            if _frames_are_similar(thumb, self._last_thumb):
                png_path.unlink(missing_ok=True)
                self._step_index -= 1
                return
            self._last_thumb = thumb
            if self._max_dim:
                _downscale_to_max_dim(png_path, self._max_dim)

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

        # The memory guard may have packed the ZIP already; never pack twice.
        if getattr(self, "_finalized_zip", None) is not None:
            return self._finalized_zip

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
# Windows fallback engine (used when psr.exe is unavailable or blocked)
# ---------------------------------------------------------------------------

class WindowsPollingRecorder(BaseRecorder):
    """Detached polling recorder for Windows when psr.exe cannot run.

    Activated silently by WindowsRecorder / Recorder() when psr.exe is missing
    (PSRNotFoundError) or refuses to launch (PermissionError, Group Policy
    block, failed elevation). Nothing about the calling contract changes: the
    caller still gets a ZIP path from start_recording() and the same ZIP back
    from stop_recording().

    Architecture: `sa start` spawns a *detached* daemon process
    (`sa record-daemon`) and returns immediately, so the recording survives the
    terminal that started it and `sa stop` - a different process entirely - can
    end it by dropping a stop sentinel into the staging directory.

    The daemon (see _PollingSession):
      * captures via Pillow ImageGrab, falling back to PowerShell/System.Drawing
      * compares each frame in memory and discards idle ones before any write
      * stops itself the moment max_screenshots is reached, packing the ZIP

    If the daemon cannot be spawned (frozen interpreter, no console), the
    session degrades to an in-process thread that honours the same sentinel.
    """

    _STATE_FILE = Path.home() / "psr_output" / ".polling_session.json"
    _INTERVAL_SECONDS = _POLL_INTERVAL_SECONDS

    def __init__(self) -> None:
        self._state: dict | None = self._load_state()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def start_recording(
        self,
        output_path: str | Path | None = None,
        max_screenshots: int = _DEFAULT_MAX_SCREENSHOTS,
        max_dim: int | None = None,
    ) -> Path:
        """Spawn the polling daemon and return the ZIP path it will write."""
        # A second `sa start` would otherwise orphan the first daemon, which
        # would keep capturing the screen until it hit its own frame cap.
        if self.has_active_session():
            try:
                WindowsPollingRecorder().stop_recording()
            except OSError:
                self._clear_state()

        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".zip")

        zip_path = safe_path(output_path)
        if zip_path.suffix.lower() != ".zip":
            zip_path = zip_path.with_suffix(".zip")
        staging = zip_path.with_suffix(".d")

        zip_path.parent.mkdir(parents=True, exist_ok=True)
        (staging / "screenshots").mkdir(parents=True, exist_ok=True)
        # A sentinel left behind by a crashed session would stop us instantly.
        try:
            (staging / _STOP_SENTINEL).unlink(missing_ok=True)
        except OSError:
            pass

        pid = self._spawn_daemon(staging, zip_path, max_screenshots, max_dim)
        self._state = {
            "mode": "polling",
            "zip": str(zip_path),
            "staging": str(staging),
            "pid": pid,
            "max_screenshots": int(max_screenshots),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_state(self._state)
        return zip_path

    def stop_recording(self) -> Path | None:
        """Signal the daemon to stop and return the ZIP once it is committed."""
        state = self._state or self._load_state()
        if state is None:
            return None

        zip_path = Path(state["zip"])
        staging = Path(state["staging"])

        # The sentinel is the cross-process stop signal.
        try:
            if staging.is_dir():
                (staging / _STOP_SENTINEL).write_text("stop", encoding="utf-8")
        except OSError:
            pass

        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)
            self._thread = None

        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if zip_path.exists():
                break
            if not staging.exists():
                break
            if not self._pid_alive(state.get("pid")):
                break
            time.sleep(0.25)

        # Daemon died mid-session: salvage whatever frames landed on disk.
        if not zip_path.exists() and staging.is_dir():
            self._salvage(staging, zip_path)

        self._clear_state()
        self._state = None
        return zip_path if zip_path.exists() else None

    def is_recording_active(self) -> bool:
        """True while a polling session is live in this or another process."""
        state = self._state or self._load_state()
        if state is None:
            return False
        if Path(state["zip"]).exists():
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        return self._pid_alive(state.get("pid"))

    @classmethod
    def has_active_session(cls) -> bool:
        """True when a polling session started by any process is still running."""
        state = cls._load_state()
        if state is None:
            return False
        if not Path(state["staging"]).is_dir():
            return False
        return cls._pid_alive(state.get("pid"))

    # ------------------------------------------------------------------
    # Daemon plumbing
    # ------------------------------------------------------------------

    def _spawn_daemon(
        self,
        staging: Path,
        zip_path: Path,
        max_screenshots: int,
        max_dim: int | None,
    ) -> int:
        """Launch `sa record-daemon` detached. Returns its PID.

        Falls back to an in-process daemon thread (returning this process's PID)
        when no interpreter is available to spawn - the session then lives only
        as long as the caller, which is still correct for library/MCP use.
        """
        cmd = [
            sys.executable, "-m", "psr_ai", "record-daemon",
            "--staging", str(staging),
            "--zip", str(zip_path),
            "--maxsc", str(max_screenshots),
            "--interval", str(self._INTERVAL_SECONDS),
        ]
        if max_dim:
            cmd += ["--max-dim", str(max_dim)]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )

        if sys.executable:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                    cwd=str(zip_path.parent),
                )
                return proc.pid
            except OSError:
                pass  # Fall through to the in-process daemon.

        self._thread = threading.Thread(
            target=run_polling_session,
            args=(staging, zip_path, max_screenshots, self._INTERVAL_SECONDS, max_dim),
            daemon=True,
        )
        self._thread.start()
        return os.getpid()

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        """Return True when the given PID is still running."""
        if not pid:
            return False
        if pid == os.getpid():
            return True
        if sys.platform == "win32":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
                )
                if not handle:
                    return False
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE
            except Exception:
                return False
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _salvage(staging: Path, zip_path: Path) -> None:
        """Pack a staging directory left behind by a daemon that never finished."""
        data_json = staging / "data.json"
        if not data_json.exists():
            shots = sorted((staging / "screenshots").glob("*.png"))
            payload = {
                "title": "Polling Recording (recovered)",
                "source": zip_path.name,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "step_count": len(shots),
                "steps": [
                    {
                        "index": i,
                        "label": f"Step {i}",
                        "timestamp": "",
                        "ui_element": "",
                        "program": "",
                        "action_text": "Screenshot",
                        "screenshot": f"screenshots/{png.name}",
                        "click_x": 0,
                        "click_y": 0,
                    }
                    for i, png in enumerate(shots)
                ],
            }
            try:
                data_json.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                return
        try:
            (staging / _STOP_SENTINEL).unlink(missing_ok=True)
        except OSError:
            pass
        _pack_to_zip(staging, zip_path)

    # ------------------------------------------------------------------
    # Session state (shared between the `sa start` and `sa stop` processes)
    # ------------------------------------------------------------------

    @classmethod
    def _load_state(cls) -> dict | None:
        try:
            data = json.loads(cls._STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "zip" not in data or "staging" not in data:
            return None
        return data

    @classmethod
    def _save_state(cls, state: dict) -> None:
        try:
            cls._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Written via a temp file so an interrupted write can never leave a
            # half-serialised state file that `sa stop` would read as "no session".
            tmp = cls._STATE_FILE.with_name(cls._STATE_FILE.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cls._STATE_FILE)
        except OSError:
            pass

    @classmethod
    def _clear_state(cls) -> None:
        try:
            cls._STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def Recorder() -> BaseRecorder:
    """Return the correct recorder engine for the current platform.

    Returns:
      WindowsPollingRecorder on win32 whenever a polling session started by an
                             earlier process is still live, so `sa stop` always
                             addresses the engine that `sa start` used
      WindowsRecorder        on win32, when psr.exe is available
      WindowsPollingRecorder on win32, silent fallback when psr.exe is
                             missing, blocked by Group Policy, or raises
                             PermissionError
      MacRecorder            on darwin
      LinuxRecorder          on linux

    Raises RuntimeError on any other unrecognised platform.
    """
    platform = sys.platform
    if platform == "win32":
        if WindowsPollingRecorder.has_active_session():
            return WindowsPollingRecorder()
        try:
            return WindowsRecorder()
        except (PSRNotFoundError, PermissionError, OSError):
            return WindowsPollingRecorder()
    if platform == "darwin":
        return MacRecorder()
    if platform.startswith("linux"):
        return LinuxRecorder()
    raise RuntimeError(
        f"Unsupported platform: {platform}. "
        "PSR.ai supports win32, darwin, and linux."
    )
