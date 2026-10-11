"""Local listener for the Hidden Context Layer browser extension.

Receives structured browser context - failed requests, console errors, and the
accessibility snapshot of clicked elements - from the MV3 extension and appends
it to the active recording session. At `sa stop` the events are correlated to
recorded steps and merged into data.json.

Everything here is stdlib: the listener must start instantly and never be the
reason a recording fails.

Endpoints (all loopback-only):
    GET  /v1/health   -> {"recording": bool, "session_id": str|None, ...}
    POST /v1/ingest   -> {"events": [...]}  ->  {"recording": bool, "stored": int}

Security:
    * binds 127.0.0.1 exclusively
    * Bearer token required unless PSR_AI_CONTEXT_OPEN=1
    * Origin must be a chrome-extension:// URL (browsers cannot forge this)
    * secrets are redacted again server-side - the extension is not trusted to
      have done it
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Hard ceilings so a runaway page cannot fill the disk or RAM.
MAX_REQUEST_BYTES = 1_048_576      # 1 MB per POST
MAX_DRAIN_BYTES = 8_388_608        # read-and-discard ceiling for rejected bodies
MAX_EVENTS_PER_REQUEST = 500
MAX_EVENTS_PER_SESSION = 20_000
MAX_STRING_CHARS = 4_000

CONTEXT_FILENAME = "context.jsonl"
META_FILENAME = "context_meta.json"

# How often the liveness file is rewritten. The extension checks in every ~30s,
# so this only throttles bursts of activity.
META_WRITE_INTERVAL = 10.0

# Beyond this, the extension is considered gone rather than merely quiet. It
# checks in on a 30-second alarm, so two missed beats is the threshold.
STALE_AFTER_SECONDS = 75.0
_TOKEN_FILE = Path.home() / ".psr_ai" / "context_token"
_STATE_FILE = Path.home() / "psr_output" / ".context_session.json"

# Values that must never be persisted in cleartext, even if the extension
# already redacted them - defence in depth against a modified extension.
_SECRET_KEY_RE = re.compile(
    r"(authorization|cookie|set[-_]?cookie|x[-_]api[-_]key|api[-_]?key|token|"
    r"password|passwd|secret|session[-_]?id|bearer)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_REDACTED = "[REDACTED]"

# Only these event kinds are accepted; anything else is dropped unread.
_ALLOWED_KINDS = frozenset({
    "network_failure",
    "console_error",
    "resource_error",
    "element_click",
})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def token_path() -> Path:
    """Return the path of the persistent pairing token."""
    return _TOKEN_FILE


def read_token() -> str | None:
    """Return the stored pairing token, or None when none has been created."""
    try:
        value = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def ensure_token(rotate: bool = False) -> str:
    """Return the pairing token, generating (or rotating) it when needed."""
    if not rotate:
        existing = read_token()
        if existing:
            return existing

    value = secrets.token_urlsafe(32)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(value, encoding="utf-8")
    try:
        os.chmod(_TOKEN_FILE, 0o600)   # No-op on Windows, correct on POSIX.
    except OSError:
        pass
    return value


def _auth_open() -> bool:
    """True when token checking is disabled for local development."""
    return os.environ.get("PSR_AI_CONTEXT_OPEN", "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def scrub_url(value: str) -> str:
    """Replace secret-looking query parameters while keeping the URL readable.

    The extension does this too, but the listener cannot assume the extension
    on the other end is the one we shipped.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(value)
        if not parts.query:
            return value
        pairs = [
            (key, _REDACTED if _SECRET_KEY_RE.search(key) else val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
        ]
        # safe="[]" keeps the marker readable as [REDACTED] rather than %5B…%5D.
        return urlunsplit(parts._replace(query=urlencode(pairs, safe="[]")))
    except ValueError:
        return value


def redact(value, _key: str = ""):
    """Recursively scrub secrets from an event payload.

    Keys whose *name* looks secret are replaced wholesale; URLs lose their
    secret query parameters; every string is scanned for JWT-shaped values and
    truncated to a sane length.
    """
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, _key) for v in value[:200]]
    if isinstance(value, str):
        if _key and _SECRET_KEY_RE.search(_key):
            return _REDACTED
        cleaned = value
        if cleaned[:8].lower().startswith(("http://", "https:/")):
            cleaned = scrub_url(cleaned)
        cleaned = _JWT_RE.sub("[REDACTED_JWT]", cleaned)
        if len(cleaned) > MAX_STRING_CHARS:
            cleaned = cleaned[:MAX_STRING_CHARS] + f"…[+{len(cleaned) - MAX_STRING_CHARS}]"
        return cleaned
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_STRING_CHARS]


# ---------------------------------------------------------------------------
# Session state (shared with the CLI across processes)
# ---------------------------------------------------------------------------

@dataclass
class ContextSession:
    """The recording session the listener is currently attached to."""

    session_id: str
    session_dir: Path
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events_received: int = 0
    # Liveness: when the extension last announced itself, and what it is. Without
    # this the listener cannot tell "the page produced no errors" apart from
    # "the extension is switched off" - the ambiguity that made the whole layer
    # feel unreliable.
    last_seen: float | None = None
    client: dict = field(default_factory=dict)

    @property
    def context_file(self) -> Path:
        return self.session_dir / CONTEXT_FILENAME

    @property
    def meta_file(self) -> Path:
        return self.session_dir / META_FILENAME

    def seconds_since_seen(self) -> float | None:
        """Seconds since the extension last checked in, or None if never."""
        if self.last_seen is None:
            return None
        return max(0.0, time.time() - self.last_seen)


class ContextStore:
    """Thread-safe append-only sink for context events."""

    def __init__(self, session: ContextSession | None = None) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._meta_written_at = 0.0
        self.attach(session)

    @property
    def session(self) -> ContextSession | None:
        with self._lock:
            return self._session

    def attach(self, session: ContextSession | None) -> None:
        with self._lock:
            self._session = session
            self._meta_written_at = 0.0
            if session is not None:
                session.session_dir.mkdir(parents=True, exist_ok=True)

    def mark_seen(self, client: dict | None = None) -> bool:
        """Record that the extension is alive. Returns False when idle.

        Called from both /v1/hello and /v1/ingest, so any sign of life counts.
        The on-disk copy is throttled - it exists so `sa stop` can still tell,
        after the listener has exited, whether the layer was ever present.
        """
        with self._lock:
            session = self._session
            if session is None:
                return False
            session.last_seen = time.time()
            if client:
                session.client = {
                    str(k)[:40]: str(v)[:80]
                    for k, v in list(client.items())[:10]
                }
            should_write = (session.last_seen - self._meta_written_at) > META_WRITE_INTERVAL
            if should_write:
                self._meta_written_at = session.last_seen
        if should_write:
            self._write_meta()
        return True

    def _write_meta(self) -> None:
        """Persist liveness so it outlives the listener process."""
        session = self.session
        if session is None:
            return
        payload = {
            "session_id": session.session_id,
            "started_at": session.started_at,
            "last_seen": session.last_seen,
            "last_seen_iso": (
                datetime.fromtimestamp(session.last_seen, timezone.utc).isoformat()
                if session.last_seen else None
            ),
            "client": session.client,
            "events_received": session.events_received,
        }
        try:
            session.session_dir.mkdir(parents=True, exist_ok=True)
            tmp = session.meta_file.with_name(session.meta_file.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, session.meta_file)
        except OSError:
            pass

    def flush_meta(self) -> None:
        """Force the liveness file to disk - called when the listener stops."""
        self._write_meta()

    def store(self, events: list[dict]) -> int:
        """Append events to the session's JSONL file. Returns how many landed."""
        with self._lock:
            session = self._session
            if session is None:
                return 0
            remaining = MAX_EVENTS_PER_SESSION - session.events_received
            if remaining <= 0:
                return 0

            written = 0
            try:
                # Re-created every batch: the session directory can be made or
                # removed by another process while a recording is in flight.
                session.session_dir.mkdir(parents=True, exist_ok=True)
                with session.context_file.open("a", encoding="utf-8") as handle:
                    for event in events[:remaining]:
                        if not isinstance(event, dict):
                            continue
                        if event.get("kind") not in _ALLOWED_KINDS:
                            continue
                        record = redact(event)
                        record.setdefault("received_at", datetime.now(timezone.utc).isoformat())
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        written += 1
            except OSError:
                return 0

            session.events_received += written
            return written


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _ContextHandler(BaseHTTPRequestHandler):
    """Minimal JSON handler. Instances get `store` and `token` from the server."""

    protocol_version = "HTTP/1.1"
    server_version = "psr-ai-context"
    sys_version = ""

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """Silence the default stderr access log - `sa` output stays clean."""

    def _origin_allowed(self) -> bool:
        """Only the extension may talk to us; web pages must not."""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True   # curl / health probes with no Origin header
        return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    def _authorized(self) -> bool:
        expected = getattr(self.server, "token", None)
        if _auth_open() or not expected:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[7:].strip(), expected)

    def _drain(self, length: int) -> None:
        """Consume an unwanted request body so keep-alive stays in sync.

        Answering before the client finishes sending desynchronises an HTTP/1.1
        connection - the client sees a connection reset instead of our status
        code. Reading and discarding (up to a ceiling) lets us reply properly.
        """
        remaining = min(max(length, 0), MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if length > MAX_DRAIN_BYTES:
            self.close_connection = True

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        origin = self.headers.get("Origin", "")
        if origin.startswith(("chrome-extension://", "moz-extension://")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ---------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(204, {})

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._send_json(403, {"error": "forbidden origin"})
            return
        if self.path.split("?")[0] != "/v1/health":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "invalid token"})
            return

        session = self.server.store.session
        seen_ago = session.seconds_since_seen() if session else None
        self._send_json(200, {
            "recording": session is not None,
            "session_id": session.session_id if session else None,
            "events_received": session.events_received if session else 0,
            "version": _package_version(),
            # Liveness, so `sa context doctor` can answer "is the extension on?"
            "extension_seen": seen_ago is not None,
            "extension_seen_seconds_ago": round(seen_ago, 1) if seen_ago is not None else None,
            "extension_live": seen_ago is not None and seen_ago <= STALE_AFTER_SECONDS,
            "client": session.client if session else {},
        })

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1

        # Every early rejection below drains the body first, so the client
        # always receives the real status code rather than a reset socket.
        if not self._origin_allowed():
            self._drain(length)
            self._send_json(403, {"error": "forbidden origin"})
            return
        route = self.path.split("?")[0]
        if route not in ("/v1/ingest", "/v1/hello"):
            self._drain(length)
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._drain(length)
            self._send_json(401, {"error": "invalid token"})
            return
        if length < 0:
            self._send_json(400, {"error": "bad content-length"})
            return
        if length == 0:
            # A bodyless hello is still a valid sign of life.
            if route == "/v1/hello":
                self._handle_hello({})
                return
            self._send_json(400, {"error": "empty body"})
            return
        if length > MAX_REQUEST_BYTES:
            self._drain(length)
            self._send_json(413, {"error": "payload too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            self._send_json(400, {"error": "invalid json"})
            return

        if route == "/v1/hello":
            self._handle_hello(payload if isinstance(payload, dict) else {})
            return

        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            self._send_json(400, {"error": "events must be a list"})
            return

        # Traffic is itself proof of life, so a busy page needs no separate hello.
        self.server.store.mark_seen(payload.get("client") if isinstance(payload, dict) else None)
        stored = self.server.store.store(events[:MAX_EVENTS_PER_REQUEST])
        session = self.server.store.session
        self._send_json(200, {
            "recording": session is not None,
            "session_id": session.session_id if session else None,
            "stored": stored,
        })

    def _handle_hello(self, payload: dict) -> None:
        """Record the extension's check-in and echo back the session state."""
        client = payload.get("client")
        self.server.store.mark_seen(client if isinstance(client, dict) else None)
        session = self.server.store.session
        self._send_json(200, {
            "recording": session is not None,
            "session_id": session.session_id if session else None,
            "acknowledged": True,
        })


def _package_version() -> str:
    try:
        from psr_ai import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "0.0.0"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ContextServer:
    """Loopback HTTP listener that feeds a ContextStore.

    Use as a context manager, or call start()/stop() directly:

        with ContextServer(store) as server:
            print(server.port)
    """

    def __init__(
        self,
        store: ContextStore | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str | None = None,
    ) -> None:
        self.store = store or ContextStore()
        self.host = host
        self._requested_port = port
        self.token = token
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._requested_port
        return self._httpd.server_address[1]

    def start(self) -> int:
        """Bind and serve in a daemon thread. Returns the bound port."""
        if self._httpd is not None:
            return self.port

        httpd = ThreadingHTTPServer((self.host, self._requested_port), _ContextHandler)
        httpd.daemon_threads = True
        httpd.timeout = 1
        # Handler instances reach these through self.server.
        httpd.store = self.store          # type: ignore[attr-defined]
        httpd.token = self.token          # type: ignore[attr-defined]

        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> ContextServer:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def serve_context_session(
    session_dir: str | Path,
    session_id: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    stop_file: str | Path | None = None,
    poll_interval: float = 0.5,
) -> Path:
    """Run a listener attached to one recording session until told to stop.

    Blocks until `stop_file` appears (the same sentinel convention the polling
    recorder uses), then returns the path of the collected context.jsonl.
    This is the body of the detached `sa context serve` process.
    """
    session_dir = Path(session_dir)
    session = ContextSession(
        session_id=session_id or session_dir.name,
        session_dir=session_dir,
    )
    store = ContextStore(session)
    sentinel = Path(stop_file) if stop_file else (session_dir / ".context_stop")

    server = ContextServer(store=store, host=host, port=port, token=token)
    bound_port = server.start()
    write_state({
        "pid": os.getpid(),
        "port": bound_port,
        "session_id": session.session_id,
        "session_dir": str(session_dir),
        "stop_file": str(sentinel),
        "started_at": session.started_at,
    })

    try:
        while not sentinel.exists():
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        # Persist the final liveness record before this process disappears -
        # `sa stop` reads it to decide whether to warn about a missing layer.
        store.flush_meta()
        try:
            sentinel.unlink(missing_ok=True)
        except OSError:
            pass
        clear_state()

    return session.context_file


# ---------------------------------------------------------------------------
# Cross-process state file
# ---------------------------------------------------------------------------

def state_path() -> Path:
    return _STATE_FILE


def write_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_name(_STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STATE_FILE)
    except OSError:
        pass


def read_state() -> dict | None:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "port" in data else None


def clear_state() -> None:
    try:
        _STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def request_stop() -> bool:
    """Drop the stop sentinel for a running listener. True if one was signalled."""
    state = read_state()
    if not state:
        return False
    stop_file = state.get("stop_file")
    if not stop_file:
        return False
    try:
        Path(stop_file).parent.mkdir(parents=True, exist_ok=True)
        Path(stop_file).write_text("stop", encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Merging captured context into data.json
# ---------------------------------------------------------------------------

def read_context_meta(session_dir: str | Path) -> dict | None:
    """Read the liveness record a listener left behind, or None.

    This is how `sa stop` distinguishes "the browser reported no problems" from
    "the extension was never running" once the listener process is gone.
    """
    path = Path(session_dir) / META_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def probe_listener(
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    token: str | None = None,
    timeout: float = 2.0,
) -> dict | None:
    """Ask a running listener for its health. None when nothing answers.

    Used by `sa context doctor`, which runs in a different process than the
    listener and therefore cannot read its in-memory state directly.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"http://{host}:{port}/v1/health")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        # Reachable but rejecting us - that is a real, reportable state.
        return {"error": "unauthorized" if exc.code in (401, 403) else f"http_{exc.code}"}
    except Exception:
        return None


def load_context_events(session_dir: str | Path) -> list[dict]:
    """Read context.jsonl, skipping any line that is not valid JSON."""
    path = Path(session_dir) / CONTEXT_FILENAME
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    except OSError:
        return []
    return events


def _parse_timestamp(value: str | None) -> float | None:
    """Parse an ISO-8601 or PSR-style timestamp into a POSIX float."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def correlate_events_to_steps(steps: list[dict], events: list[dict]) -> dict[int, list[dict]]:
    """Assign each event to the step it happened during.

    An event belongs to the last step that started at or before it. Events that
    precede the first step (page errors from before recording caught up) are
    attached to step 0 so nothing is silently lost.
    """
    if not steps:
        return {}

    step_times: list[tuple[float, int]] = []
    for index, step in enumerate(steps):
        moment = _parse_timestamp(step.get("timestamp"))
        if moment is not None:
            step_times.append((moment, step.get("index", index)))
    if not step_times:
        return {}
    step_times.sort()

    buckets: dict[int, list[dict]] = {}
    for event in events:
        moment = _parse_timestamp(event.get("timestamp") or event.get("received_at"))
        if moment is None:
            continue
        target = step_times[0][1]
        for start, step_index in step_times:
            if start <= moment:
                target = step_index
            else:
                break
        buckets.setdefault(target, []).append(event)
    return buckets


def summarize_events(events: list[dict]) -> dict:
    """Compact per-step view: what failed, what errored, what was clicked."""
    network = [e for e in events if e.get("kind") == "network_failure"]
    console = [e for e in events if e.get("kind") == "console_error"]
    resource = [e for e in events if e.get("kind") == "resource_error"]
    clicks = [e for e in events if e.get("kind") == "element_click"]

    def _compact(fields: dict) -> dict:
        """Drop empty fields - every null costs the reader (and the LLM) tokens."""
        return {k: v for k, v in fields.items() if v not in (None, "", [], {})}

    summary: dict = {}
    if network:
        summary["network_failures"] = [
            _compact({
                "method": e.get("method"),
                "url": e.get("url"),
                "status": e.get("status"),
                "error": e.get("error"),
                "response_body": e.get("response_body"),
            })
            for e in network
        ]
    if console:
        summary["console_errors"] = [
            _compact({
                "subtype": e.get("subtype"),
                "message": e.get("message"),
                "source": e.get("source"),
                "stack": e.get("stack"),
            })
            for e in console
        ]
    if resource:
        summary["resource_errors"] = [
            {"url": e.get("url"), "tag": e.get("tag")} for e in resource
        ]
    if clicks:
        latest = clicks[-1]
        summary["element"] = latest.get("element")
        summary["page_url"] = latest.get("page_url")
        summary["page_title"] = latest.get("page_title")
    return summary


def merge_context_into_data(
    data: dict,
    session_dir: str | Path,
    events: list[dict] | None = None,
) -> int:
    """Attach browser context to each step of a parsed session, in place.

    Returns the number of events merged. Adds:
      step["browser_context"]      per-step failures/errors/element
      data["browser_context"]      session-level totals
    """
    events = load_context_events(session_dir) if events is None else events
    steps = data.get("steps") or []
    if not events:
        return 0

    buckets = correlate_events_to_steps(steps, events)
    merged = 0
    for step in steps:
        bucket = buckets.get(step.get("index"))
        if not bucket:
            continue
        summary = summarize_events(bucket)
        if summary:
            step["browser_context"] = summary
            merged += len(bucket)

    kinds: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind", "unknown"))
        kinds[kind] = kinds.get(kind, 0) + 1

    data["browser_context"] = {
        "events_total": len(events),
        "events_merged": merged,
        "by_kind": kinds,
        "source": "psr-ai hidden context layer (MV3 extension)",
    }
    return merged
