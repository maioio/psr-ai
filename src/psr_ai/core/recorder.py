"""Controls PSR.exe: resolve the executable, start/stop recordings, query process state."""

from __future__ import annotations

import importlib.resources
import subprocess
import shutil
from pathlib import Path

from psr_ai.utils.paths import default_output_dir, resolve_env_path, safe_path, timestamp_filename

# PSR process name as it appears in the Windows process list.
_PSR_PROCESS_NAME = "psr.exe"

# Bundled fallback asset directory, relative to the installed package root.
# Layout (editable install):  <repo>/assets/psr_fallback/
# Layout (site-packages):     resolved via importlib.resources at runtime.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_ASSET_DIR = _REPO_ROOT / "assets" / "psr_fallback"
_BUNDLED_LNK = _BUNDLED_ASSET_DIR / "psr_shortcut.lnk"

# Friendly name for the portable standalone file that could optionally be
# placed in assets/psr_fallback/ by the user as a manual recovery step.
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
    """Raised when psr.exe cannot be located through any resolution tier."""


def _resolve_bundled_asset_dir() -> Path:
    """Return the asset directory using importlib.resources for installed layouts.

    For an editable install the directory is next to the repo root.
    For a wheel install the data_files land under the package data prefix.
    Falls back to the relative path computed from __file__ if both fail.
    """
    # Try importlib.resources first (works for wheel installs with package_data).
    try:
        # Python 3.9+: importlib.resources.files is the modern API.
        pkg_files = importlib.resources.files("psr_ai")
        # assets/ is at the repo root, not inside the package source tree,
        # so we navigate up from the package anchor.
        candidate = Path(str(pkg_files)).parent.parent / "assets" / "psr_fallback"
        if candidate.is_dir():
            return candidate
    except (TypeError, AttributeError, ModuleNotFoundError):
        pass

    # Fallback: relative path computed from this source file.
    return _BUNDLED_ASSET_DIR


class Recorder:
    """Wraps PSR.exe to start/stop screen recordings and query recording state.

    Resolution order for psr.exe (first match wins):
      Fast path : shutil.which("psr")                          - PATH lookup
      Tier 1    : %SystemRoot%\\System32\\psr.exe              - standard install
      Tier 2    : %WinDir%\\System32\\psr.exe                  - alt env var
      Tier 3a   : assets/psr_fallback/psr.exe (portable exe)  - manual recovery
      Tier 3b   : assets/psr_fallback/psr_shortcut.lnk        - Shell shortcut
    """

    def __init__(self) -> None:
        self._psr_exe: Path = self._resolve_psr()
        self._pending_output: Path | None = None

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_psr(self) -> Path:
        """Return the path to use when launching PSR, trying all tiers in order."""
        # Fast path: PATH lookup.
        found = shutil.which("psr")
        if found:
            return Path(found)

        # Tier 1: %SystemRoot%\System32\psr.exe
        try:
            tier1 = resolve_env_path("SystemRoot", "System32", "psr.exe")
            if tier1.exists():
                return tier1
        except KeyError:
            pass

        # Tier 2: %WinDir%\System32\psr.exe
        try:
            tier2 = resolve_env_path("WinDir", "System32", "psr.exe")
            if tier2.exists():
                return tier2
        except KeyError:
            pass

        # Tier 3a: portable exe bundled in assets/psr_fallback/psr.exe.
        # Resolved via importlib.resources so it works in both editable and
        # wheel installs.
        asset_dir = _resolve_bundled_asset_dir()
        bundled_exe = asset_dir / _BUNDLED_EXE_NAME
        if bundled_exe.exists():
            return bundled_exe

        # Tier 3b: .lnk shortcut - Windows Shell resolves it at launch time.
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
        """Launch PSR.exe in the background and return the ZIP output path.

        PSR runs asynchronously: Popen returns immediately so the terminal
        stays free. The recording continues until stop_recording() is called.
        """
        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".zip")

        out = safe_path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self._pending_output = out

        subprocess.Popen(
            [
                str(self._psr_exe),
                "/start",
                "/output", str(out),
                "/sc", "1",
                "/maxsc", str(max_screenshots),
                "/gui", "0",
            ],
            shell=self._use_shell,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return out

    def stop_recording(self) -> Path | None:
        """Send the /stop signal to PSR.exe and wait for it to commit the ZIP."""
        subprocess.run(
            [str(self._psr_exe), "/stop"],
            shell=self._use_shell,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            check=False,
        )
        return self._pending_output

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

    # ------------------------------------------------------------------
    # Backwards-compatible aliases
    # ------------------------------------------------------------------

    def start(self, output_path: str | Path | None = None, max_screenshots: int = 25) -> Path:
        return self.start_recording(output_path=output_path, max_screenshots=max_screenshots)

    def stop(self) -> Path | None:
        return self.stop_recording()
