"""End-to-end self-test for the Hidden Context Layer.

Serves a page that deliberately breaks in the three ways the layer is meant to
catch, opens it in the user's browser, and reports which signals actually made
it back to the listener.

This exists because the failure mode of the layer is *silence*. A recording
with no `browser_context` looks exactly the same whether the page behaved
perfectly or the extension was switched off. Running a known-bad page turns
that ambiguity into a yes or no.

The logic mirrors the harness used to validate the layer during development;
only the reporting is different.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from psr_ai.core.context_server import load_context_events

# A high port unlikely to collide with a dev server the user is running.
SELFTEST_PORT = 8799

# Markers the page emits, so a check can never be satisfied by unrelated
# traffic that happened to be captured at the same moment.
MARKER = "PSR_SELFTEST"

_PAGE = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>PSR.ai context self-test</title>
<style>
  body {{ font: 15px/1.6 system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.2rem; }}
  code {{ background: rgba(128,128,128,.15); padding: .1rem .3rem; border-radius: 3px; }}
  #done {{ color: #2a7; font-weight: 600; }}
</style>
</head>
<body>
  <h1>PSR.ai context self-test</h1>
  <p>This page just failed a request and logged a console error on purpose.</p>
  <form aria-label="Self test">
    <button id="selftest-button" type="button" data-testid="selftest">{MARKER} button</button>
  </form>
  <p id="done">You can close this tab - results are in your terminal.</p>
  <script>
    fetch('/api/{MARKER}', {{ method: 'POST' }}).catch(function () {{}});
    console.error('{MARKER}: deliberate console error from the self-test page');
    // A synthetic click cannot be trusted by the page, so the button is here
    // for the user to click if the click signal needs checking by hand.
  </script>
</body>
</html>"""

_ERROR_BODY = json.dumps({"error": f"{MARKER} deliberate failure", "code": 500}).encode()


class _SelfTestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: A003
        """Stay silent - `sa` output is a result stream, not a web log."""

    def do_GET(self) -> None:  # noqa: N802
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        # The deliberate 500 the layer must capture, body included.
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_ERROR_BODY)))
        self.end_headers()
        self.wfile.write(_ERROR_BODY)


@dataclass
class SelfTestResult:
    """Outcome of one self-test run."""

    checks: dict[str, bool] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    @property
    def score(self) -> str:
        return f"{sum(self.checks.values())}/{len(self.checks)} signals"


def _evaluate(events: list[dict]) -> dict[str, bool]:
    """Map captured events onto the three signals the layer promises."""
    return {
        "failed request captured (500)": any(
            e.get("kind") == "network_failure" and e.get("status") == 500
            and MARKER in str(e.get("url", ""))
            for e in events
        ),
        "response body captured": any(
            MARKER in str(e.get("response_body", "")) for e in events
        ),
        "console error captured": any(
            e.get("kind") == "console_error" and MARKER in str(e.get("message", ""))
            for e in events
        ),
    }


def _hints_for(checks: dict[str, bool]) -> list[str]:
    """Turn failures into the next action, not just a red mark."""
    hints: list[str] = []
    if not any(checks.values()):
        hints.append(
            "Nothing arrived at all - the extension is probably not loaded or is disabled. "
            "Run `sa context doctor`, then check chrome://extensions."
        )
        return hints
    if not checks.get("response body captured") and checks.get("failed request captured (500)"):
        hints.append(
            "The request was seen but not its body: the MAIN-world script did not run. "
            "Reload the page and re-run - this can happen on the first load after the browser starts."
        )
    if not checks.get("console error captured"):
        hints.append(
            "The console error was missed. This is usually the service worker cold-starting "
            "during page load; reload the test page and run `sa context test` again."
        )
    return hints


def run_self_test(
    session_dir: str | Path,
    timeout: float = 25.0,
    port: int = SELFTEST_PORT,
    keep_open: bool = False,
    open_browser: bool = True,
    echo: Callable[[str], None] | None = None,
) -> SelfTestResult:
    """Serve the known-bad page, open it, and report which signals arrived.

    Only events produced after this call are considered, so a previous run
    cannot make a broken layer look healthy.
    """
    say = echo or (lambda _msg: None)
    session_dir = Path(session_dir)

    baseline = len(load_context_events(session_dir))

    server = ThreadingHTTPServer(("127.0.0.1", port), _SelfTestHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/"
    say(f"  Test page: {url}")

    try:
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                say("  Could not open a browser automatically - open the URL above by hand.")

        deadline = time.monotonic() + timeout
        fresh: list[dict] = []
        checks: dict[str, bool] = {}
        while time.monotonic() < deadline:
            fresh = load_context_events(session_dir)[baseline:]
            checks = _evaluate(fresh)
            if all(checks.values()):
                break
            time.sleep(0.5)

        if not checks:
            checks = _evaluate(fresh)

        return SelfTestResult(checks=checks, events=fresh, hints=_hints_for(checks))
    finally:
        if not keep_open:
            server.shutdown()
            server.server_close()
