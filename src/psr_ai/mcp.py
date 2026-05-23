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
_SERVER_VERSION   = "0.1.0"
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
    raise ValueError(f"Unimplemented tool: {name}")


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


def _tool_psr_fetch_link(args: dict[str, Any]) -> str:
    """Fetch structured content from a URL and return a clean text block.

    Social/video URLs (YouTube, Instagram, TikTok) are handled by yt-dlp
    (subprocess call). All other URLs are fetched with urllib and HTML tags
    are stripped to produce readable plain text.
    """
    import re
    import shutil
    import subprocess
    import urllib.request
    from html.parser import HTMLParser

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
        if ytdlp is None:
            return (
                "yt-dlp is not installed or not on PATH. "
                "Install it with: pip install yt-dlp\n"
                f"URL: {url}"
            )
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
            if not output and result.returncode != 0:
                return f"yt-dlp error (exit {result.returncode}):\n{result.stderr.strip()[:1000]}"
            return output[:max_chars]
        except subprocess.TimeoutExpired:
            return f"yt-dlp timed out fetching: {url}"

    # Generic URL: fetch HTML and strip tags.
    class _StripHTML(HTMLParser):
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

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; psr-ai/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            html = resp.read(512_000).decode(charset, errors="replace")
    except Exception as exc:
        return f"Failed to fetch {url}: {exc}"

    parser = _StripHTML()
    parser.feed(html)
    text = parser.text()

    # Collapse repeated blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)

    header = f"URL: {url}\n\n"
    body = text[:max_chars]
    return header + body


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
