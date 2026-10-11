"""Launch a browser with the Hidden Context Layer already loaded.

This removes the manual install step - but only on browsers that still honour
`--load-extension`. Measured on this machine, 2026-08:

    Playwright's bundled Chromium 148  ->  loads the extension
    Microsoft Edge 151                 ->  loads the extension
    Google Chrome 151                  ->  SILENTLY IGNORES the flag

Chrome accepts the argument, prints no error, and starts without the
extension. That is a deliberate hardening against malware side-loading, and no
combination of --enable-automation, --test-type, --disable-extensions-except or
--enable-unsafe-extension-debugging brings it back (all tested). Chrome is
therefore treated as manual-install-only: `sa context install --no-launch`
prints the directory to drop into chrome://extensions once.

So the launcher prefers a browser where the automatic path genuinely works,
and says so plainly instead of opening a window that quietly captures nothing.

The browser runs on a dedicated PSR.ai profile under ~/.psr_ai/browser_profile:

  * the everyday browser is untouched, and may keep running
  * logins persist between sessions, so the second run is already signed in
  * the layer is confined to this profile instead of following the user
    everywhere

Known constraint: extensions do not load in headless Chromium at all - verified
here, zero service workers register under --headless=new. The launcher is
headed only, which is what a recording session needs anyway.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROFILE_DIR = Path.home() / ".psr_ai" / "browser_profile"

# Which browsers still honour --load-extension. Chrome accepts the flag and
# ignores it (measured on 151), so auto-launching Chrome would open a window
# that silently captures nothing - worse than telling the truth up front.
AUTO_LOAD_SUPPORT = {
    "Edge": True,
    "Brave": True,     # Chromium-based and lags Chrome's hardening; unverified here
    "Chrome": False,   # verified ignored on Chrome 151
}

# Flags that keep the instrumented browser quiet and predictable.
_BASE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-features=Translate",
]


@dataclass
class BrowserTarget:
    """A browser PSR.ai knows how to instrument."""

    name: str
    executable: Path
    profile_dir: Path

    @property
    def supports_auto_load(self) -> bool:
        """True when this browser actually honours --load-extension."""
        return AUTO_LOAD_SUPPORT.get(self.name, False)

    def __str__(self) -> str:
        return f"{self.name} ({self.executable})"


def _windows_candidates() -> list[tuple[str, list[str]]]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    return [
        ("Chrome", [
            rf"{program_files}\Google\Chrome\Application\chrome.exe",
            rf"{program_files_x86}\Google\Chrome\Application\chrome.exe",
            rf"{local_app_data}\Google\Chrome\Application\chrome.exe",
        ]),
        ("Edge", [
            rf"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
            rf"{program_files}\Microsoft\Edge\Application\msedge.exe",
        ]),
        ("Brave", [
            rf"{program_files}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{program_files_x86}\BraveSoftware\Brave-Browser\Application\brave.exe",
        ]),
    ]


def _macos_candidates() -> list[tuple[str, list[str]]]:
    return [
        ("Chrome", ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]),
        ("Edge", ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
        ("Brave", ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]),
    ]


def _linux_candidates() -> list[tuple[str, list[str]]]:
    return [
        ("Chrome", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]),
        ("Edge", ["microsoft-edge", "microsoft-edge-stable"]),
        ("Brave", ["brave-browser"]),
    ]


def find_browsers(profile_dir: Path | None = None) -> list[BrowserTarget]:
    """Return every Chromium-family browser installed on this machine."""
    if sys.platform == "win32":
        candidates = _windows_candidates()
    elif sys.platform == "darwin":
        candidates = _macos_candidates()
    else:
        candidates = _linux_candidates()

    base_profile = profile_dir or DEFAULT_PROFILE_DIR
    found: list[BrowserTarget] = []
    for name, paths in candidates:
        for candidate in paths:
            if not candidate:
                continue
            resolved = Path(candidate) if os.path.sep in str(candidate) else None
            if resolved is None:
                located = shutil.which(str(candidate))
                resolved = Path(located) if located else None
            if resolved and resolved.exists():
                found.append(BrowserTarget(
                    name=name,
                    executable=resolved,
                    # One profile per browser so Chrome and Edge never collide.
                    profile_dir=base_profile / name.lower(),
                ))
                break

    # Browsers that can actually auto-load the extension come first, so the
    # default choice is one where the automatic path works.
    found.sort(key=lambda b: not b.supports_auto_load)
    return found


def find_browser(preferred: str | None = None, profile_dir: Path | None = None) -> BrowserTarget | None:
    """Return the browser to instrument: the preferred one, else the first found."""
    browsers = find_browsers(profile_dir)
    if not browsers:
        return None
    if preferred:
        wanted = preferred.strip().lower()
        for browser in browsers:
            if browser.name.lower() == wanted:
                return browser
        return None
    return browsers[0]


def extension_dir() -> Path:
    """Return the bundled extension directory (source tree or installed wheel)."""
    import importlib.resources
    try:
        return Path(str(importlib.resources.files("psr_ai") / "extension"))
    except (TypeError, AttributeError, ModuleNotFoundError):
        return Path(__file__).resolve().parent.parent / "extension"


def build_launch_command(
    browser: BrowserTarget,
    url: str | None = None,
    extension: Path | None = None,
) -> list[str]:
    """Build the argv that starts the browser with the context layer loaded."""
    ext = extension or extension_dir()
    args = [
        str(browser.executable),
        f"--user-data-dir={browser.profile_dir}",
        f"--disable-extensions-except={ext}",
        f"--load-extension={ext}",
        *_BASE_FLAGS,
    ]
    if url:
        args.append(url)
    return args


class UnsupportedBrowserError(RuntimeError):
    """Raised when the chosen browser would ignore --load-extension."""


def launch(
    preferred: str | None = None,
    url: str | None = None,
    profile_dir: Path | None = None,
    extension: Path | None = None,
    allow_unsupported: bool = False,
) -> tuple[BrowserTarget, int]:
    """Start an instrumented browser. Returns (browser, pid).

    Raises FileNotFoundError when no supported browser is installed,
    UnsupportedBrowserError when the chosen browser ignores --load-extension,
    and OSError when the browser cannot be started.
    """
    browser = find_browser(preferred, profile_dir)
    if browser is None:
        installed = ", ".join(b.name for b in find_browsers(profile_dir)) or "none"
        raise FileNotFoundError(
            f"No supported browser found (looked for Chrome, Edge, Brave; found: {installed}). "
            "Load the extension manually instead - run `sa context install` for the path."
        )

    if not browser.supports_auto_load and not allow_unsupported:
        capable = [b.name for b in find_browsers(profile_dir) if b.supports_auto_load]
        hint = (
            f"Use {capable[0]} instead (sa context browser --browser {capable[0].lower()})"
            if capable else
            "Install Microsoft Edge, or load the extension once via chrome://extensions"
        )
        raise UnsupportedBrowserError(
            f"{browser.name} ignores --load-extension, so the layer would capture nothing. "
            f"{hint}. Run `sa context install --no-launch` for the manual one-time steps."
        )

    ext = extension or extension_dir()
    if not (ext / "manifest.json").exists():
        raise FileNotFoundError(f"Extension files not found at {ext}")

    browser.profile_dir.mkdir(parents=True, exist_ok=True)

    creationflags = 0
    if sys.platform == "win32":
        # Detached: the browser must outlive the `sa` process that started it.
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )

    process = subprocess.Popen(
        build_launch_command(browser, url=url, extension=ext),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return browser, process.pid
