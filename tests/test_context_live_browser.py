"""Live proof: a real browser, the real extension, the real listener.

Opt-in, because it needs Playwright, a real Chromium-family browser, and a
visible window - extensions do not load in headless Chrome (verified: zero
service workers register under --headless=new).

Run it with:

    PSR_AI_LIVE_BROWSER=1 pytest tests/test_context_live_browser.py -v

This is the test that proves the whole chain: page -> content scripts ->
service worker -> HTTP -> listener -> context.jsonl.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from psr_ai.core import context_browser as cb
from psr_ai.core import context_server as cs

pytestmark = pytest.mark.skipif(
    not os.environ.get("PSR_AI_LIVE_BROWSER"),
    reason="live browser test: set PSR_AI_LIVE_BROWSER=1 to run (opens a window)",
)

PAGE_PORT = 8793

PAGE = """<!doctype html>
<html><head><title>PSR live test</title></head><body>
<form aria-label="Checkout">
  <button id="order" class="primary" type="button" data-testid="place-order">Place order</button>
</form>
<script>
  fetch('/api/orders', {method: 'POST'}).catch(function () {});
  console.error('OrderService exploded: TypeError reading orderId');
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = b'{"error":"payment gateway timeout"}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def captured_events():
    """Drive a real browser through one failing interaction; return the events."""
    playwright = pytest.importorskip("playwright.sync_api", reason="needs playwright")
    if not cb.find_browsers():
        pytest.skip("no Chromium-family browser installed")

    os.environ["PSR_AI_CONTEXT_OPEN"] = "1"   # skip token pairing for the test

    session_dir = Path(tempfile.mkdtemp(prefix="psr_live_")) / "session_output"
    store = cs.ContextStore(cs.ContextSession(session_id="live", session_dir=session_dir))
    listener = cs.ContextServer(store=store, port=cs.DEFAULT_PORT, token=None)
    listener.start()

    page_server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), _Handler)
    threading.Thread(target=page_server.serve_forever, daemon=True).start()

    profile = Path(tempfile.mkdtemp(prefix="psr_profile_"))
    try:
        with playwright.sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,                      # extensions need a real window
                args=[
                    f"--load-extension={cb.extension_dir()}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            try:
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{PAGE_PORT}/", wait_until="load")
                page.click("#order")               # a real, trusted click

                deadline = time.time() + 20
                events: list[dict] = []
                while time.time() < deadline:
                    events = cs.load_context_events(session_dir)
                    if len({e.get("kind") for e in events}) >= 3:
                        break
                    time.sleep(0.5)
            finally:
                context.close()
        yield events
    finally:
        listener.stop()
        page_server.shutdown()
        os.environ.pop("PSR_AI_CONTEXT_OPEN", None)


def test_events_reach_the_listener(captured_events):
    assert captured_events, "no browser context reached the listener"


def test_failed_request_is_captured(captured_events):
    failures = [e for e in captured_events if e.get("kind") == "network_failure"]
    assert any(e.get("status") == 500 for e in failures)


def test_response_body_is_captured(captured_events):
    """The MAIN-world fetch wrapper recovers what webRequest cannot expose."""
    bodies = [str(e.get("response_body", "")) for e in captured_events]
    assert any("payment gateway timeout" in body for body in bodies)


def test_console_error_is_captured(captured_events):
    errors = [e for e in captured_events if e.get("kind") == "console_error"]
    assert any("OrderService" in str(e.get("message", "")) for e in errors)


def test_clicked_element_accessibility_is_captured(captured_events):
    clicks = [e for e in captured_events if e.get("kind") == "element_click"]
    assert clicks, "no element_click captured"
    element = clicks[-1].get("element") or {}
    assert element.get("role") == "button"
    assert element.get("name") == "Place order"
    assert "order" in (element.get("selector") or "")
