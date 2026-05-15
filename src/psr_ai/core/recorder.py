"""Controls PSR.exe: start/stop recording and resolve the executable path."""

from __future__ import annotations

import subprocess
import shutil
from datetime import datetime
from pathlib import Path

from psr_ai.utils.paths import default_output_dir, safe_path

# Bundled shortcut shipped in assets/psr_fallback/ as a last-resort launcher.
# Windows resolves .lnk files natively via ShellExecute, so Popen can use it
# the same way it would use the .exe when invoked through the shell.
_ASSETS_DIR = Path(__file__).parent.parent.parent.parent / "assets" / "psr_fallback"
_BUNDLED_LNK = _ASSETS_DIR / "Steps Recorder.lnk"


class PSRNotFoundError(RuntimeError):
    """Raised when psr.exe cannot be located on the system."""


class Recorder:
    """Thin wrapper around PSR.exe for starting and stopping screen recordings."""

    # 4-tier fallback resolver for psr.exe location (Pitfall 3):
    # some Windows 11 builds and GPO policies strip `psr` from PATH.
    # Tier 4: bundled .lnk shortcut in assets/psr_fallback/ (ships with this package).
    _FALLBACK_PATHS = [
        Path(r"C:\Windows\System32\psr.exe"),
        Path(r"C:\Windows\SysWOW64\psr.exe"),
    ]

    def __init__(self) -> None:
        self._psr_exe = self._resolve_psr()
        self._pending_output: Path | None = None

    def _resolve_psr(self) -> Path:
        """Return the path to psr.exe (or the bundled .lnk fallback), trying 4 sources."""
        found = shutil.which("psr")
        if found:
            return Path(found)
        for candidate in self._FALLBACK_PATHS:
            if candidate.exists():
                return candidate
        # Tier 4: launch via the bundled .lnk shortcut.
        # Windows ShellExecute resolves the shortcut to the real psr.exe at runtime.
        if _BUNDLED_LNK.exists():
            return _BUNDLED_LNK
        raise PSRNotFoundError(
            "psr.exe not found via PATH, System32, SysWOW64, or bundled shortcut.\n"
            "It should be in C:\\Windows\\System32\\psr.exe.\n"
            "On some enterprise builds it may be blocked by Group Policy."
        )

    @property
    def _use_shell(self) -> bool:
        """Return True when launching via a .lnk shortcut (requires shell resolution)."""
        return self._psr_exe.suffix.lower() == ".lnk"

    def start(self, output_path: str | None = None, max_screenshots: int = 25) -> Path:
        """Launch PSR.exe and return the path where the ZIP will be saved."""
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(out_dir / f"psr_{timestamp}.zip")

        out = safe_path(output_path)
        self._pending_output = out

        args = [
            str(self._psr_exe),
            "/start",
            "/output", str(out),
            "/sc", "1",
            "/maxsc", str(max_screenshots),
            "/gui", "0",
        ]
        subprocess.Popen(
            args,
            shell=self._use_shell,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return out

    def stop(self) -> Path | None:
        """Send the stop signal to PSR.exe."""
        subprocess.Popen(
            [str(self._psr_exe), "/stop"],
            shell=self._use_shell,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return self._pending_output
