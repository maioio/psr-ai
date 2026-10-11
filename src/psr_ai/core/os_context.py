"""OS-level context: active window title and (opt-in) clipboard content.

Both are appended to the final data.json so Claude can tell *what the user was
actually looking at* and *what they had just copied* - the two pieces of context
a screenshot never carries.

Clipboard capture is deliberately opt-in. A clipboard routinely holds passwords,
tokens, and private messages that have nothing to do with the recording, so it
is captured only when explicitly requested (`sa stop --clipboard`), is scrubbed
with the same redaction rules as browser context, and is truncated.

No third-party dependencies: Windows goes through ctypes/user32, macOS through
pbpaste, Linux through wl-paste/xclip/xsel.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

MAX_CLIPBOARD_CHARS = 2000
MAX_TITLE_CHARS = 300

# Anything matching these is replaced before the value reaches data.json.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}.*?-{5}END [A-Z ]*PRIVATE KEY-{5}", re.DOTALL),
     "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+:[^\s:@]{6,}@[A-Za-z0-9.-]+\b"), "[REDACTED_CREDENTIALS]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CARD_NUMBER]"),
]

# A clipboard that looks like nothing but a secret is dropped entirely.
_SECRET_ONLY_RE = re.compile(r"^[A-Za-z0-9_\-./+=]{32,}$")


@dataclass
class OSContext:
    """OS-level metadata captured at a point in time."""

    captured_at: str
    active_window: str = ""
    clipboard: str | None = None
    clipboard_captured: bool = False
    clipboard_redacted: bool = False
    platform: str = sys.platform

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def redact_secrets(text: str) -> tuple[str, bool]:
    """Scrub known secret shapes. Returns (clean_text, was_redacted)."""
    if not text:
        return text, False
    cleaned = text
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if cleaned == text and _SECRET_ONLY_RE.match(text.strip()):
        # One long opaque token and nothing else: almost certainly a credential.
        return "[REDACTED_OPAQUE_VALUE]", True
    return cleaned, cleaned != text


# ---------------------------------------------------------------------------
# Active window title
# ---------------------------------------------------------------------------

def active_window_title() -> str:
    """Return the foreground window's title, or "" when it cannot be read."""
    try:
        if sys.platform == "win32":
            return _active_window_win32()
        if sys.platform == "darwin":
            return _active_window_darwin()
        return _active_window_linux()
    except Exception:
        return ""


def _active_window_win32() -> str:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()[:MAX_TITLE_CHARS]


def _active_window_darwin() -> str:
    script = (
        'tell application "System Events" to get name of first process '
        "whose frontmost is true"
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False, timeout=5
    )
    return result.stdout.strip()[:MAX_TITLE_CHARS]


def _active_window_linux() -> str:
    if shutil.which("xdotool"):
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.stdout.strip():
            return result.stdout.strip()[:MAX_TITLE_CHARS]

    if shutil.which("xprop"):
        root = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        parts = root.stdout.split()
        if parts and re.fullmatch(r"0x[0-9a-fA-F]+", parts[-1]):
            named = subprocess.run(
                ["xprop", "-id", parts[-1], "_NET_WM_NAME"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if "=" in named.stdout:
                return named.stdout.split("=", 1)[1].strip().strip('"')[:MAX_TITLE_CHARS]
    return ""


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def clipboard_text() -> str:
    """Return the clipboard's text content, or "" when unavailable.

    Reads only text formats - image and file-list clipboards return "" rather
    than dragging binary data into the session.
    """
    try:
        if sys.platform == "win32":
            return _clipboard_win32()
        if sys.platform == "darwin":
            return _clipboard_darwin()
        return _clipboard_linux()
    except Exception:
        return ""


def _clipboard_win32() -> str:
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    # The clipboard is a shared global resource; another app may hold it.
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.c_wchar_p(pointer).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_darwin() -> str:
    result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False, timeout=5)
    return result.stdout


def _clipboard_linux() -> str:
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if not shutil.which(command[0]):
            continue
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
        if result.returncode == 0:
            return result.stdout
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_os_context(include_clipboard: bool = False) -> OSContext:
    """Capture OS-level context.

    The active window title is always collected - it is what makes a screenshot
    interpretable. The clipboard is collected only when include_clipboard is
    True, and is redacted and truncated before it is returned.
    """
    context = OSContext(
        captured_at=datetime.now(timezone.utc).isoformat(),
        active_window=active_window_title(),
    )

    if include_clipboard:
        raw = clipboard_text()
        if raw:
            cleaned, was_redacted = redact_secrets(raw)
            if len(cleaned) > MAX_CLIPBOARD_CHARS:
                cleaned = cleaned[:MAX_CLIPBOARD_CHARS] + f"…[+{len(cleaned) - MAX_CLIPBOARD_CHARS} chars]"
            context.clipboard = cleaned
            context.clipboard_redacted = was_redacted
        else:
            context.clipboard = ""
        context.clipboard_captured = True

    return context


def enrich_data_json(data: dict, include_clipboard: bool = False) -> dict:
    """Append an `os_context` block to a parsed session dict, in place."""
    data["os_context"] = collect_os_context(include_clipboard=include_clipboard).to_dict()
    return data
