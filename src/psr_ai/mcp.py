"""MCP (Model Context Protocol) JSON-RPC server for PSR.ai.

Reads newline-delimited JSON-RPC 2.0 messages from stdin and writes
responses to stdout. No external dependencies are required beyond the
stdlib; Pillow is used only inside psr_quick_snapshot and is imported
lazily so the server starts without it.

Entry point:
    sa mcp           (console script added to cli.py)
    python -m psr_ai.mcp

Spec reference: https://spec.modelcontextprotocol.io/specification/
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Module-level imports so tests can patch them via "psr_ai.mcp.<name>".
# Each is imported lazily (first use) to keep the server startup fast on
# environments that don't have every optional dependency installed.
try:
    from psr_ai.core.recorder import Recorder
except Exception:  # pragma: no cover
    Recorder = None  # type: ignore[assignment,misc]

try:
    from psr_ai.core.parser import parse_session, generate_json_meta
except Exception:  # pragma: no cover
    parse_session = None  # type: ignore[assignment]
    generate_json_meta = None  # type: ignore[assignment]

try:
    from psr_ai.core.generator import MarkdownGenerator
except Exception:  # pragma: no cover
    MarkdownGenerator = None  # type: ignore[assignment]

try:
    from psr_ai.utils.paths import default_output_dir
except Exception:  # pragma: no cover
    default_output_dir = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Server identity
# ---------------------------------------------------------------------------

_SERVER_NAME      = "psr-ai"

try:  # Single source of truth for the version - never let these drift.
    from psr_ai import __version__ as _SERVER_VERSION
except Exception:  # pragma: no cover
    _SERVER_VERSION = "0.0.0"

_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Snapshot mitigation constants
# ---------------------------------------------------------------------------

_SNAPSHOT_MAX_PX = 1280   # longest edge after downsampling
_SNAPSHOT_COLORS = 128    # palette size for quantization

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "psr_start_recording",
        "description": (
            "Start a PSR.ai screen-recording session in the background. "
            "Invokes the platform recorder (psr.exe on Windows, screencapture on "
            "macOS, gnome-screenshot/scrot/maim on Linux). "
            "Returns a text confirmation once recording has been initiated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": (
                        "Explicit path for the output ZIP file. "
                        "Omit to use the default timestamped file in ~/psr_output/."
                    ),
                },
                "max_screenshots": {
                    "type": "integer",
                    "description": "Maximum screenshots the recorder will capture. Default 100.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "psr_stop_and_parse",
        "description": (
            "Stop the active PSR.ai recording session, parse the output ZIP/MHT "
            "archive, render report.md, and return the full AI context block "
            "text from that report directly for consumption."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "psr_quick_snapshot",
        "description": (
            "Capture a single screenshot of the current screen, downsample it "
            "to a maximum of 1280px on the longest edge, quantize to 128 colors, "
            "and return the result as a Base64-encoded PNG string. "
            "Token-efficient: suitable for direct multimodal feeding to a vision model. "
            "Requires Pillow (pip install psr-ai[gif]) and a platform screenshot tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_px": {
                    "type": "integer",
                    "description": (
                        "Longest-edge pixel cap after downsampling. Default 1280."
                    ),
                },
                "colors": {
                    "type": "integer",
                    "description": (
                        "Palette size for quantization (2-256). Default 128."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "psr_open_context_browser",
        "description": (
            "Open a browser with the PSR.ai Hidden Context Layer already loaded, so "
            "failed network requests (4xx/5xx), uncaught console errors, and the "
            "accessibility snapshot of every clicked element are captured silently - "
            "with no DevTools window open and no debugger infobar. "
            "The extension ships inside psr-ai; nothing is installed from a web store. "
            "Uses a dedicated PSR.ai browser profile, so the user's everyday browser is "
            "untouched and logins in this profile persist between sessions. "
            "Call this before psr_start_recording when the task involves debugging a "
            "web page, then ask the user to reproduce the problem in the window that opens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "browser": {
                    "type": "string",
                    "description": "Which browser to launch: chrome, edge, or brave. Default: first installed.",
                },
                "url": {
                    "type": "string",
                    "description": "Optional http:// or https:// URL to open immediately.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "psr_get_browser_context",
        "description": (
            "Return the browser context captured so far by the Hidden Context Layer: "
            "failed network requests with their response bodies, uncaught console errors "
            "with stack traces, and the accessibility snapshot (role, accessible name, "
            "states, selector) of clicked elements. "
            "Use this while a recording is running to see what actually broke in the "
            "page, instead of guessing from a screenshot. Returns a compact JSON summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        "Filter to one kind: network_failure, console_error, "
                        "resource_error, or element_click. Omit for everything."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Return at most this many of the most recent events. Default 50.",
                },
                "session_dir": {
                    "type": "string",
                    "description": "Read a finished session directory instead of the live one.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "psr_context_status",
        "description": (
            "Report whether the Hidden Context Layer listener is running, which "
            "recording session it is attached to, how many browser-context events it "
            "has captured, and whether a supported browser is available to instrument. "
            "Use this to check the layer is live before telling the user to reproduce a bug."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "psr_fetch_link",
        "description": (
            "Fetch structured content from a URL and return a clean text block. "
            "For YouTube, Instagram, or TikTok URLs: uses yt-dlp (must be installed) "
            "to extract video metadata and available transcript/subtitles. "
            "For all other URLs: fetches the page body via urllib and strips HTML "
            "tags to return readable plain text. No external Python packages are "
            "required for generic pages; yt-dlp is optional and invoked as a "
            "subprocess so its absence only affects social/video URLs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch. Must be a fully-qualified http/https address.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of body text to return. "
                        "Default 8000. Use lower values to save tokens on large pages."
                    ),
                },
            },
            "required": ["url"],
        },
    },
]

_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _TOOLS)


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _send(obj: dict[str, Any]) -> None:
    """Serialize obj as JSON and write it to stdout followed by a newline."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(request_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": request_id, "error": err})


# Standard JSON-RPC error codes
_PARSE_ERROR      = -32700
_INVALID_REQUEST  = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS   = -32602
_INTERNAL_ERROR   = -32603


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(request_id: Any, params: dict[str, Any]) -> None:
    """Respond to the MCP initialize handshake."""
    _ok(
        request_id,
        {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": _SERVER_NAME,
                "version": _SERVER_VERSION,
            },
        },
    )


def _handle_tools_list(request_id: Any, params: dict[str, Any]) -> None:
    """Return the list of tools this server exposes."""
    _ok(request_id, {"tools": _TOOLS})


def _handle_tools_call(request_id: Any, params: dict[str, Any]) -> None:
    """Dispatch a tool call and return a content-block result."""
    name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments") or {}

    if name not in _TOOL_NAMES:
        _error(
            request_id,
            _METHOD_NOT_FOUND,
            f"Unknown tool: {name}",
            {"available": sorted(_TOOL_NAMES)},
        )
        return

    try:
        result_text = _dispatch_tool(name, arguments)
    except (TypeError, ValueError) as exc:
        _error(request_id, _INVALID_PARAMS, str(exc))
        return
    except Exception as exc:
        _error(request_id, _INTERNAL_ERROR, f"Tool execution failed: {exc}")
        return

    _ok(
        request_id,
        {
            "content": [
                {"type": "text", "text": result_text},
            ],
            "isError": False,
        },
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict[str, Any]) -> str:
    """Call the matching tool function and return a text result."""
    if name == "psr_start_recording":
        return _tool_psr_start_recording(args)
    if name == "psr_stop_and_parse":
        return _tool_psr_stop_and_parse(args)
    if name == "psr_quick_snapshot":
        return _tool_psr_quick_snapshot(args)
    if name == "psr_fetch_link":
        return _tool_psr_fetch_link(args)
    if name == "psr_open_context_browser":
        return _tool_psr_open_context_browser(args)
    if name == "psr_get_browser_context":
        return _tool_psr_get_browser_context(args)
    if name == "psr_context_status":
        return _tool_psr_context_status(args)
    raise ValueError(f"Unimplemented tool: {name}")


# ---------------------------------------------------------------------------
# Hidden Context Layer tools
# ---------------------------------------------------------------------------

def _tool_psr_open_context_browser(args: dict[str, Any]) -> str:
    """Launch an instrumented browser and describe what was opened."""
    from psr_ai.core import context_browser

    try:
        browser, pid = context_browser.launch(
            preferred=args.get("browser") or None,
            url=args.get("url") or None,
        )
    except ValueError as exc:
        return f"Could not open an instrumented browser: {exc}"
    except FileNotFoundError as exc:
        return (
            f"Could not open an instrumented browser: {exc}\n"
            "Ask the user to run `sa context install --no-launch` and load the "
            "printed directory via chrome://extensions instead."
        )
    except OSError as exc:
        return f"Could not start the browser: {exc}"

    return (
        f"{browser.name} is open with the Hidden Context Layer loaded (pid {pid}).\n"
        f"Profile: {browser.profile_dir}\n"
        "Failed requests, console errors, and clicked-element accessibility data are "
        "now captured silently while a recording session is active.\n"
        "Next: call psr_start_recording, then ask the user to reproduce the problem "
        "in that window."
    )


def _tool_psr_context_status(args: dict[str, Any]) -> str:
    """Report listener state, event count, and available browsers."""
    from pathlib import Path

    from psr_ai.core import context_browser, context_server

    state = context_server.read_state()
    browsers = ", ".join(b.name for b in context_browser.find_browsers()) or "none found"

    if not state:
        return (
            "Hidden Context Layer: listener NOT running.\n"
            f"Browsers available to instrument: {browsers}\n"
            "Start a recording with psr_start_recording (or `sa start`) to bring it up."
        )

    session_dir = Path(state.get("session_dir", ""))
    events = context_server.load_context_events(session_dir)
    counts: dict[str, int] = {}
    for event in events:
        counts[str(event.get("kind"))] = counts.get(str(event.get("kind")), 0) + 1

    # Liveness matters more than the event count: zero events with a live
    # extension means a clean page, zero events without one means no capture.
    health = context_server.probe_listener(
        port=int(state.get("port", context_server.DEFAULT_PORT)),
        token=context_server.read_token(),
    )
    if health and health.get("extension_live"):
        client = health.get("client") or {}
        who = " ".join(filter(None, [client.get("browser"), client.get("version")])) or "browser"
        extension_line = (
            f"Extension: LIVE ({who}, last check-in "
            f"{health.get('extension_seen_seconds_ago')}s ago)"
        )
    elif health and health.get("extension_seen"):
        extension_line = (
            f"Extension: STALE (last seen {health.get('extension_seen_seconds_ago')}s ago) - "
            "the browser may be closed or the extension disabled."
        )
    else:
        extension_line = (
            "Extension: NOT DETECTED - nothing has checked in. Browser context will be "
            "empty until the user enables it at chrome://extensions, or opens a browser "
            "with psr_open_context_browser. Tell the user to run `sa context doctor`."
        )

    return (
        "Hidden Context Layer: listening on "
        f"127.0.0.1:{state.get('port')} (pid {state.get('pid')})\n"
        f"{extension_line}\n"
        f"Session: {state.get('session_id')}\n"
        f"Directory: {session_dir}\n"
        f"Events captured: {len(events)} {counts or ''}\n"
        f"Browsers available to instrument: {browsers}"
    )


def _tool_psr_get_browser_context(args: dict[str, Any]) -> str:
    """Return captured browser context as compact JSON."""
    from pathlib import Path

    from psr_ai.core import context_server

    session_dir_arg = args.get("session_dir")
    if session_dir_arg:
        session_dir = Path(str(session_dir_arg))
    else:
        state = context_server.read_state()
        if not state:
            return (
                "No live recording session. Start one with psr_start_recording, "
                "or pass session_dir to read a finished session."
            )
        session_dir = Path(state.get("session_dir", ""))

    events = context_server.load_context_events(session_dir)
    kind = args.get("kind")
    if kind:
        events = [e for e in events if e.get("kind") == kind]

    try:
        limit = int(args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    if limit > 0:
        events = events[-limit:]

    if not events:
        return (
            f"No browser context captured yet in {session_dir}.\n"
            "Check psr_context_status, and make sure the user is working in the "
            "browser opened by psr_open_context_browser."
        )

    counts: dict[str, int] = {}
    for event in events:
        counts[str(event.get("kind"))] = counts.get(str(event.get("kind")), 0) + 1

    payload = {
        "session_dir": str(session_dir),
        "returned": len(events),
        "by_kind": counts,
        "events": events,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tool_psr_start_recording(args: dict[str, Any]) -> str:
    """Invoke the platform recorder and return a confirmation string."""
    from pathlib import Path

    output_path_arg = args.get("output_path")
    max_screenshots = int(args.get("max_screenshots", 100))

    output_path: Path | None = (
        Path(output_path_arg).expanduser().resolve()
        if output_path_arg
        else None
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    rec = Recorder()
    out = rec.start_recording(output_path=output_path, max_screenshots=max_screenshots)

    return (
        f"PSR.ai recording started successfully.\n"
        f"Output path: {out}\n"
        f"Max screenshots: {max_screenshots}\n"
        f"Run psr_stop_and_parse when finished."
    )


def _tool_psr_stop_and_parse(args: dict[str, Any]) -> str:
    """Stop the active recording, parse it, and return the report text."""
    import time
    from pathlib import Path

    rec = Recorder()
    zip_path: Path | None = rec.stop_recording()

    # PSR writes the ZIP asynchronously on Windows; wait up to 10 s for it.
    if zip_path is not None:
        zip_path = Path(zip_path)
        deadline = time.monotonic() + 10.0
        while not zip_path.exists() and time.monotonic() < deadline:
            time.sleep(0.5)

    if zip_path is None or not zip_path.exists():
        return (
            "No active recording found or the output ZIP was not written in time. "
            "Ensure psr_start_recording was called before psr_stop_and_parse."
        )

    session      = parse_session(zip_path)
    output_dir   = default_output_dir(zip_path)
    json_path    = generate_json_meta(session, output_dir / "data.json")
    report_path  = MarkdownGenerator(json_path).write(output_dir)

    report_text = report_path.read_text(encoding="utf-8")

    header = (
        f"Recording stopped and parsed.\n"
        f"Output directory: {output_dir}\n"
        f"Steps captured: {len(session.steps)}\n"
        f"---\n"
    )
    return header + report_text


def _tool_psr_quick_snapshot(args: dict[str, Any]) -> str:
    """Capture screen, downsample, quantize, return Base64 PNG."""
    import base64
    import io
    import subprocess
    import tempfile
    from pathlib import Path

    max_px = int(args.get("max_px", _SNAPSHOT_MAX_PX))
    colors = int(args.get("colors", _SNAPSHOT_COLORS))
    colors = max(2, min(256, colors))

    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for psr_quick_snapshot. "
            "Install it with: pip install psr-ai[gif]"
        ) from exc

    raw_png = _capture_screen_bytes()

    img = Image.open(io.BytesIO(raw_png)).convert("RGB")

    # Downsample: preserve aspect ratio, cap longest edge at max_px.
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.LANCZOS,
        )

    # Quantize to palette of `colors` entries (reduces token cost significantly).
    img = img.quantize(colors=colors).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return b64


def _capture_screen_bytes() -> bytes:
    """Take a screenshot using the best available platform method.

    Returns raw PNG bytes. Raises RuntimeError when no capture tool is found.
    """
    import io
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    # Windows: use PIL/Pillow ImageGrab when available (preferred, no subprocess).
    if sys.platform == "win32":
        try:
            from PIL import ImageGrab
            import io as _io
            buf = _io.BytesIO()
            ImageGrab.grab().save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            pass

    # macOS: screencapture -x writes to a temp file.
    if sys.platform == "darwin":
        if shutil.which("screencapture"):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["screencapture", "-x", str(tmp_path)],
                    check=True,
                    capture_output=True,
                )
                return tmp_path.read_bytes()
            finally:
                tmp_path.unlink(missing_ok=True)

    # Linux: try gnome-screenshot, scrot, maim in order.
    if sys.platform.startswith("linux"):
        for tool, cmd_tpl in [
            ("gnome-screenshot", ["gnome-screenshot", "-f", "{p}"]),
            ("scrot",            ["scrot",            "{p}"]),
            ("maim",             ["maim",             "{p}"]),
        ]:
            if shutil.which(tool):
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                cmd = [c.format(p=str(tmp_path)) for c in cmd_tpl]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    return tmp_path.read_bytes()
                finally:
                    tmp_path.unlink(missing_ok=True)

    # Final fallback for any platform: PIL ImageGrab (requires Pillow + display).
    try:
        from PIL import ImageGrab
        buf = io.BytesIO()
        ImageGrab.grab().save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        pass

    raise RuntimeError(
        "No screenshot tool found. "
        "On Windows/macOS Pillow ImageGrab is used; "
        "on Linux install gnome-screenshot, scrot, or maim."
    )


def _strip_html(html: str) -> str:
    """Strip HTML tags and return readable plain text.

    Skips script/style/noscript blocks entirely. Collapses excess blank lines.
    """
    import re
    from html.parser import HTMLParser

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in ("script", "style", "noscript"):
                self._skip = True

        def handle_endtag(self, tag: str) -> None:
            if tag in ("script", "style", "noscript"):
                self._skip = False

        def handle_data(self, data: str) -> None:
            if not self._skip:
                stripped = data.strip()
                if stripped:
                    self._parts.append(stripped)

        def text(self) -> str:
            return "\n".join(self._parts)

    p = _Parser()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", p.text())


_LOGIN_WALL_SIGNALS = (
    "please enable javascript",
    "enable cookies",
    "verify you are human",
    "your connection was blocked",
)

# Signals that indicate the *entire page* is a login gate (not just nav links).
# Checked only when the body is short, to avoid false positives on pages that
# contain "log in" only in their navigation footer.
_LOGIN_GATE_SIGNALS = (
    "sign up to",
    "create an account",
    "access denied",
)

_BROWSER_FALLBACK_MIN_CHARS = 200


def _needs_browser_fallback(text: str, http_status: int | None) -> bool:
    """Return True when the fetch likely hit a JS wall or hard auth gate.

    A page that merely contains navigation "Log in" / "Sign in" links is NOT
    considered a login wall -- only pages where those phrases dominate the
    content (i.e. the body is short) or where harder signals are present.
    """
    if http_status is not None and http_status in (401, 403, 429):
        return True
    lower = text.lower()
    # Hard signals: always trigger regardless of length.
    if any(signal in lower for signal in _LOGIN_WALL_SIGNALS):
        return True
    # Soft gate signals: only trigger when the page body is thin.
    if len(text) < _BROWSER_FALLBACK_MIN_CHARS:
        if any(signal in lower for signal in _LOGIN_GATE_SIGNALS):
            return True
        return True  # thin page with no real content
    return False


def _resolve_user_data_dir() -> str | None:
    """Return the local Chrome or Edge user data directory path, or None.

    Tries Chrome first, then Edge. Returns None on non-Windows/macOS or when
    neither directory exists. The caller falls back to an isolated profile when
    None is returned.
    """
    import os
    import sys

    candidates: list[str] = []

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
        ]
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
        ]

    for path in candidates:
        if os.path.isdir(path):
            return path

    return None


def _fetch_with_browser(url: str, max_chars: int) -> str:
    """Fetch a page using a Playwright browser, reusing the local user session.

    Strategy:
      1. Attempt launch_persistent_context with the local Chrome/Edge user data
         directory so existing authenticated sessions (cookies, localStorage) are
         available. Uses the "Default" profile inside that directory.
      2. If the user data dir is not found or the persistent launch fails, fall
         back to a plain headless Chromium with no session.

    Returns the visible body text (document.body.innerText after a 2-second
    JS-settle wait). Raises ImportError when Playwright is not installed.
    """
    import re

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for browser-based fetching. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from exc

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    user_data_dir = _resolve_user_data_dir()
    text = ""

    with sync_playwright() as pw:
        ctx = None
        browser = None

        if user_data_dir is not None:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=True,
                    user_agent=_UA,
                    args=["--profile-directory=Default"],
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2000)
                text = page.evaluate("() => document.body.innerText") or ""
            except Exception:
                # Persistent context failed (e.g. profile locked by running Chrome).
                # Close whatever opened and fall through to the isolated fallback.
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                ctx = None
                text = ""

        if not text and ctx is None:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_UA)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)
            text = page.evaluate("() => document.body.innerText") or ""

        if ctx is not None:
            ctx.close()
        if browser is not None:
            browser.close()

    return re.sub(r"\n{3,}", "\n\n", text.strip())[:max_chars]


def _tool_psr_fetch_link(args: dict[str, Any]) -> str:
    """Fetch structured content from a URL and return a clean text block.

    Fetch strategy (automatic, in order):
      1. Social/video URLs (YouTube, Instagram, TikTok): yt-dlp subprocess.
      2. All other URLs: urllib static fetch + HTML strip.
      3. Auto-fallback: if the static response is thin, gated, or blocked
         (login wall, JS-only SPA, HTTP 403/429), retry with a headless
         Chromium browser via Playwright.

    Playwright and yt-dlp are optional; their absence is reported gracefully.
    """
    import re
    import shutil
    import subprocess
    import urllib.request
    from urllib.error import HTTPError

    url: str = args.get("url", "").strip()
    if not url:
        raise ValueError("url argument is required")

    max_chars = int(args.get("max_chars", 8000))
    max_chars = max(100, min(100_000, max_chars))

    _SOCIAL_PATTERNS = (
        r"youtube\.com/",
        r"youtu\.be/",
        r"instagram\.com/",
        r"tiktok\.com/",
    )
    is_social = any(re.search(p, url, re.IGNORECASE) for p in _SOCIAL_PATTERNS)

    if is_social:
        ytdlp = shutil.which("yt-dlp")
        ytdlp_failure: str | None = None

        if ytdlp is not None:
            cmd = [
                ytdlp,
                "--skip-download",
                "--write-info-json",
                "--write-auto-sub",
                "--sub-format", "vtt",
                "--print", "%(title)s\n%(uploader)s\n%(description)s\n%(webpage_url)s",
                "--no-playlist",
                url,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                output = result.stdout.strip()
                if output and result.returncode == 0:
                    return output[:max_chars]
                # yt-dlp failed or returned empty output; record reason and fall through.
                ytdlp_failure = result.stderr.strip()[:500] or f"exit code {result.returncode}"
            except subprocess.TimeoutExpired:
                ytdlp_failure = "yt-dlp timed out"
        else:
            ytdlp_failure = "yt-dlp is not installed"

        # yt-dlp unavailable or failed: try browser fallback with local session.
        try:
            browser_text = _fetch_with_browser(url, max_chars)
            note = f"[yt-dlp unavailable: {ytdlp_failure}; used browser fallback]\n\n"
            if _needs_browser_fallback(browser_text, None):
                note += "[Note: page may require authentication]\n\n"
            return f"URL: {url}\n\n{note}{browser_text}"
        except ImportError as exc:
            return (
                f"yt-dlp failed ({ytdlp_failure}) and browser fallback is unavailable: {exc}\n"
                f"Install Playwright: pip install playwright && playwright install chromium\n"
                f"URL: {url}"
            )
        except Exception as exc:
            return (
                f"yt-dlp failed ({ytdlp_failure}) and browser fallback also failed: {exc}\n"
                f"URL: {url}"
            )

    # Static fetch via urllib.
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; psr-ai/1.0)"},
    )
    http_status: int | None = None
    static_text = ""
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            http_status = resp.status
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read(512_000).decode(charset, errors="replace")
        static_text = _strip_html(html)
    except HTTPError as exc:
        http_status = exc.code
    except Exception as exc:
        return f"Failed to fetch {url}: {exc}"

    # Check whether we need to escalate to the browser fallback.
    if _needs_browser_fallback(static_text, http_status):
        try:
            browser_text = _fetch_with_browser(url, max_chars)
            if _needs_browser_fallback(browser_text, None):
                # Both methods returned thin content; return best effort.
                note = "[Note: page may require authentication or is JS-heavy]\n\n"
                body = browser_text or static_text
                return f"URL: {url}\n\n{note}{body[:max_chars]}"
            return f"URL: {url}\n\n{browser_text}"
        except ImportError as exc:
            # Playwright not installed: fall through to static result with notice.
            notice = (
                f"[Browser fallback unavailable: {exc}]\n"
                f"[Static fetch returned limited content ({len(static_text)} chars)]\n\n"
            )
            return f"URL: {url}\n\n{notice}{static_text[:max_chars]}"
        except Exception as exc:
            notice = f"[Browser fallback failed: {exc}]\n\n"
            return f"URL: {url}\n\n{notice}{static_text[:max_chars]}"

    return f"URL: {url}\n\n{static_text[:max_chars]}"


# ---------------------------------------------------------------------------
# Request router
# ---------------------------------------------------------------------------

_METHOD_HANDLERS = {
    "initialize":  _handle_initialize,
    "tools/list":  _handle_tools_list,
    "tools/call":  _handle_tools_call,
}


def _route(message: dict[str, Any]) -> None:
    """Validate and dispatch one JSON-RPC message."""
    request_id = message.get("id")

    if message.get("jsonrpc") != "2.0":
        _error(request_id, _INVALID_REQUEST, "jsonrpc field must be '2.0'")
        return

    method = message.get("method")
    if not isinstance(method, str) or not method:
        _error(request_id, _INVALID_REQUEST, "Missing or invalid 'method' field")
        return

    # Notifications (id absent) are silently dropped; we have none to handle.
    if request_id is None:
        return

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        _error(request_id, _METHOD_NOT_FOUND, f"Method not found: {method}")
        return

    params: dict[str, Any] = message.get("params") or {}
    handler(request_id, params)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop(*, stdin=None) -> None:
    """Read newline-delimited JSON-RPC 2.0 messages and dispatch each one.

    Blocks until stdin is closed (EOF). Each line must be one complete JSON
    object. Binary-mode stdin is supported; lines are decoded as UTF-8.

    Parameter:
        stdin - readable stream (default: sys.stdin); injectable for tests.
                Patch sys.stdout before calling to redirect server output.
    """
    _in = stdin or sys.stdin

    for raw_line in _in:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace").strip()
        else:
            line = raw_line.strip()

        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _error(None, _PARSE_ERROR, f"Parse error: {exc}")
            continue

        if not isinstance(message, dict):
            _error(None, _INVALID_REQUEST, "Request must be a JSON object")
            continue

        try:
            _route(message)
        except Exception as exc:
            _error(message.get("id"), _INTERNAL_ERROR, f"Internal error: {exc}")


def main() -> None:
    """Console-script entry point (also called by `sa mcp`)."""
    main_loop()


if __name__ == "__main__":
    main()
