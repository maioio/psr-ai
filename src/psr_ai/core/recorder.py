"""Controls PSR.exe: resolve the executable, start/stop recordings, query process state."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from psr_ai.utils.paths import default_output_dir, resolve_env_path, safe_path, timestamp_filename

# Bundled .lnk shortcut shipped in assets/psr_fallback/.
# Used as Tier 3 when psr.exe is not reachable via absolute system paths.
# File layout: <package-root>/assets/psr_fallback/psr_shortcut.lnk
# __file__ is src/psr_ai/core/recorder.py, so we go up 4 levels to reach the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_LNK = _REPO_ROOT / "assets" / "psr_fallback" / "psr_shortcut.lnk"

# PSR process name as it appears in the Windows process list.
_PSR_PROCESS_NAME = "psr.exe"


class PSRNotFoundError(RuntimeError):
    """Raised when psr.exe cannot be located through any available tier."""


class Recorder:
    """Wraps PSR.exe to start/stop screen recordings and query recording state.

    Resolution order for psr.exe:
      Fast path : shutil.which("psr")              - PATH lookup, works when Scripts/ is configured
      Tier 1    : %SystemRoot%\\System32\\psr.exe  - primary absolute path on all standard installs
      Tier 2    : %WinDir%\\System32\\psr.exe      - handles non-standard SystemRoot env configs
      Tier 3    : assets/psr_fallback/psr_shortcut.lnk - last resort for locked-down environments
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

        # Tier 3: bundled .lnk shortcut - Windows Shell resolves it at launch time.
        if _BUNDLED_LNK.exists():
            return _BUNDLED_LNK

        raise PSRNotFoundError(
            "psr.exe not found. Tried: PATH lookup, %SystemRoot%\\System32, "
            "%WinDir%\\System32, and the bundled shortcut in assets/psr_fallback/.\n"
            "On enterprise machines psr.exe may be blocked by Group Policy."
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

        output_path is sanitized via safe_path() before being passed to PSR.
        If not provided, a timestamped filename is generated automatically.
        """
        if output_path is None:
            out_dir = default_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / timestamp_filename("psr", ".zip")

        out = safe_path(output_path)
        # Ensure the parent directory exists before PSR tries to write the ZIP.
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
        """Send the /stop signal to PSR.exe and wait for it to commit the ZIP.

        Uses subprocess.run (blocking) so callers can rely on the ZIP being
        fully written to disk before this method returns.
        """
        subprocess.run(
            [str(self._psr_exe), "/stop"],
            shell=self._use_shell,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            check=False,  # PSR /stop exits non-zero on some builds; treat as success.
        )
        return self._pending_output

    def is_recording_active(self) -> bool:
        """Return True if a psr.exe process is currently running on this machine.

        Uses `tasklist` (always present on Windows) rather than psutil so
        there is no additional runtime dependency.
        """
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {_PSR_PROCESS_NAME}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return _PSR_PROCESS_NAME.lower() in result.stdout.lower()
        except FileNotFoundError:
            # tasklist unavailable - cannot determine state.
            return False

    # ------------------------------------------------------------------
    # Backwards-compatible aliases used by cli.py
    # ------------------------------------------------------------------

    def start(self, output_path: str | Path | None = None, max_screenshots: int = 25) -> Path:
        """Alias for start_recording() - keeps the CLI wiring clean."""
        return self.start_recording(output_path=output_path, max_screenshots=max_screenshots)

    def stop(self) -> Path | None:
        """Alias for stop_recording() - keeps the CLI wiring clean."""
        return self.stop_recording()
