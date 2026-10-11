"""Tests for the Hidden Context Layer listener, redaction, and step correlation."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from psr_ai.core import context_server as cs

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
TOKEN = "unit-test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    """A listener bound to an ephemeral port, attached to a temp session."""
    session = cs.ContextSession(session_id="unit", session_dir=tmp_path / "session_output")
    store = cs.ContextStore(session)
    instance = cs.ContextServer(store=store, port=0, token=TOKEN)
    instance.start()
    yield instance
    instance.stop()


def _request(url, payload=None, token=TOKEN, origin=EXT_ORIGIN, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        err.read()
        return err.code, {}


def _event(**overrides) -> dict:
    base = {
        "kind": "console_error",
        "subtype": "uncaught_exception",
        "timestamp": "2026-08-04T10:00:05Z",
        "message": "TypeError: x is undefined",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

class TestToken:

    def test_ensure_token_is_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_TOKEN_FILE", tmp_path / "token")
        first = cs.ensure_token()
        assert first
        assert cs.ensure_token() == first

    def test_rotate_produces_a_new_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_TOKEN_FILE", tmp_path / "token")
        first = cs.ensure_token()
        assert cs.ensure_token(rotate=True) != first

    def test_read_token_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_TOKEN_FILE", tmp_path / "absent")
        assert cs.read_token() is None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestHttpSurface:

    def test_health_reports_active_recording(self, server):
        status, body = _request(f"http://127.0.0.1:{server.port}/v1/health")
        assert status == 200
        assert body["recording"] is True
        assert body["session_id"] == "unit"

    def test_health_reports_idle_when_detached(self, server):
        server.store.attach(None)
        status, body = _request(f"http://127.0.0.1:{server.port}/v1/health")
        assert status == 200
        assert body["recording"] is False
        assert body["session_id"] is None

    def test_bad_token_is_rejected(self, server):
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/health", token="nope")
        assert status == 401

    def test_missing_token_is_rejected(self, server):
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/health", token=None)
        assert status == 401

    def test_web_page_origin_is_rejected(self, server):
        status, _ = _request(
            f"http://127.0.0.1:{server.port}/v1/health", origin="https://evil.example.com"
        )
        assert status == 403

    def test_unknown_route_is_404(self, server):
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/nope")
        assert status == 404

    def test_ingest_stores_events(self, server):
        status, body = _request(
            f"http://127.0.0.1:{server.port}/v1/ingest", {"events": [_event(), _event()]}
        )
        assert status == 200
        assert body["stored"] == 2
        assert server.store.session.events_received == 2

    def test_ingest_drops_unknown_event_kinds(self, server):
        status, body = _request(
            f"http://127.0.0.1:{server.port}/v1/ingest",
            {"events": [_event(), {"kind": "exfiltrate", "data": "x"}]},
        )
        assert status == 200
        assert body["stored"] == 1

    def test_ingest_requires_an_events_list(self, server):
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/ingest", {"events": "nope"})
        assert status == 400

    def test_oversized_payload_is_rejected_with_413(self, server):
        status, _ = _request(
            f"http://127.0.0.1:{server.port}/v1/ingest",
            {"events": [_event(message="x" * 2_000_000)]},
        )
        assert status == 413

    def test_nothing_is_stored_without_a_session(self, server):
        server.store.attach(None)
        status, body = _request(f"http://127.0.0.1:{server.port}/v1/ingest", {"events": [_event()]})
        assert status == 200
        assert body["stored"] == 0
        assert body["recording"] is False

    def test_open_mode_skips_token_check(self, server, monkeypatch):
        monkeypatch.setenv("PSR_AI_CONTEXT_OPEN", "1")
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/health", token=None)
        assert status == 200

    def test_preflight_is_answered(self, server):
        status, _ = _request(f"http://127.0.0.1:{server.port}/v1/ingest", method="OPTIONS")
        assert status == 204


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedaction:

    def test_secret_named_keys_are_replaced(self):
        result = cs.redact({"authorization": "Bearer abc123", "message": "fine"})
        assert result["authorization"] == "[REDACTED]"
        assert result["message"] == "fine"

    def test_jwt_is_scrubbed_anywhere_in_a_string(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"  # gitleaks:allow
        result = cs.redact({"message": f"failed with {jwt} attached"})
        assert jwt not in result["message"]
        assert "[REDACTED_JWT]" in result["message"]

    def test_secret_query_parameters_are_scrubbed(self):
        result = cs.redact({"url": "https://api.example.com/x?api_key=SUPERSECRET&page=2"})
        assert "SUPERSECRET" not in result["url"]
        assert "page=2" in result["url"]

    def test_non_secret_urls_survive_intact(self):
        url = "https://example.com/orders/42?page=2&sort=desc"
        assert cs.redact({"url": url})["url"] == url

    def test_long_strings_are_truncated(self):
        result = cs.redact({"message": "x" * (cs.MAX_STRING_CHARS + 500)})
        assert len(result["message"]) < cs.MAX_STRING_CHARS + 60

    def test_nested_structures_are_walked(self):
        payload = {"element": {"attributes": {"token": "secret-value"}}}
        assert cs.redact(payload)["element"]["attributes"]["token"] == "[REDACTED]"

    def test_stored_events_are_redacted_on_disk(self, server, tmp_path):
        _request(
            f"http://127.0.0.1:{server.port}/v1/ingest",
            {"events": [_event(kind="network_failure",
                               url="https://api.example.com/x?token=LEAKME",
                               status=500)]},
        )
        raw = server.store.session.context_file.read_text(encoding="utf-8")
        assert "LEAKME" not in raw
        assert "network_failure" in raw


# ---------------------------------------------------------------------------
# Correlation and merge
# ---------------------------------------------------------------------------

def _steps() -> list[dict]:
    return [
        {"index": 0, "label": "Step 0", "timestamp": "2026-08-04T10:00:00Z"},
        {"index": 1, "label": "Step 1", "timestamp": "2026-08-04T10:00:10Z"},
        {"index": 2, "label": "Step 2", "timestamp": "2026-08-04T10:00:20Z"},
    ]


class TestCorrelation:

    def test_event_lands_on_the_step_it_happened_during(self):
        events = [_event(timestamp="2026-08-04T10:00:12Z")]
        buckets = cs.correlate_events_to_steps(_steps(), events)
        assert list(buckets) == [1]

    def test_event_before_the_first_step_is_kept_on_step_zero(self):
        events = [_event(timestamp="2026-08-04T09:59:00Z")]
        buckets = cs.correlate_events_to_steps(_steps(), events)
        assert list(buckets) == [0]

    def test_event_after_the_last_step_lands_on_the_last_step(self):
        events = [_event(timestamp="2026-08-04T11:00:00Z")]
        buckets = cs.correlate_events_to_steps(_steps(), events)
        assert list(buckets) == [2]

    def test_psr_style_timestamps_are_understood(self):
        steps = [{"index": 0, "label": "Step 0", "timestamp": "08/04/2026 10:00:00 AM"}]
        buckets = cs.correlate_events_to_steps(steps, [_event(timestamp="2026-08-04T10:00:05Z")])
        assert list(buckets) == [0]

    def test_events_without_timestamps_are_skipped(self):
        buckets = cs.correlate_events_to_steps(_steps(), [{"kind": "console_error"}])
        assert buckets == {}

    def test_no_steps_means_no_buckets(self):
        assert cs.correlate_events_to_steps([], [_event()]) == {}


class TestMerge:

    def _session_with(self, tmp_path: Path, events: list[dict]) -> Path:
        session_dir = tmp_path / "session_output"
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / cs.CONTEXT_FILENAME).open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        return session_dir

    def test_merge_attaches_context_to_the_right_step(self, tmp_path):
        session_dir = self._session_with(tmp_path, [
            _event(kind="network_failure", timestamp="2026-08-04T10:00:11Z",
                   url="https://api.example.com/orders", status=503, method="GET"),
        ])
        data = {"steps": _steps()}
        merged = cs.merge_context_into_data(data, session_dir)
        assert merged == 1
        assert "browser_context" not in data["steps"][0]
        assert data["steps"][1]["browser_context"]["network_failures"][0]["status"] == 503

    def test_merge_adds_a_session_level_summary(self, tmp_path):
        session_dir = self._session_with(tmp_path, [
            _event(timestamp="2026-08-04T10:00:01Z"),
            _event(kind="network_failure", timestamp="2026-08-04T10:00:02Z", status=404),
        ])
        data = {"steps": _steps()}
        cs.merge_context_into_data(data, session_dir)
        summary = data["browser_context"]
        assert summary["events_total"] == 2
        assert summary["by_kind"]["console_error"] == 1
        assert summary["by_kind"]["network_failure"] == 1

    def test_clicked_element_is_exposed_on_the_step(self, tmp_path):
        session_dir = self._session_with(tmp_path, [
            {
                "kind": "element_click", "timestamp": "2026-08-04T10:00:11Z",
                "page_url": "https://shop.example.com/cart",
                "element": {"tag": "button", "role": "button", "name": "Place order"},
            },
        ])
        data = {"steps": _steps()}
        cs.merge_context_into_data(data, session_dir)
        assert data["steps"][1]["browser_context"]["element"]["name"] == "Place order"

    def test_merge_is_a_noop_without_a_context_file(self, tmp_path):
        data = {"steps": _steps()}
        assert cs.merge_context_into_data(data, tmp_path) == 0
        assert "browser_context" not in data

    def test_corrupt_lines_are_skipped(self, tmp_path):
        session_dir = tmp_path / "session_output"
        session_dir.mkdir()
        (session_dir / cs.CONTEXT_FILENAME).write_text(
            json.dumps(_event(timestamp="2026-08-04T10:00:11Z")) + "\n{ this is not json\n",
            encoding="utf-8",
        )
        data = {"steps": _steps()}
        assert cs.merge_context_into_data(data, session_dir) == 1


# ---------------------------------------------------------------------------
# Cross-process state
# ---------------------------------------------------------------------------

class TestState:

    def test_write_read_clear_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_STATE_FILE", tmp_path / "state.json")
        cs.write_state({"pid": 1, "port": 8765, "stop_file": str(tmp_path / ".stop")})
        assert cs.read_state()["port"] == 8765
        cs.clear_state()
        assert cs.read_state() is None

    def test_read_state_rejects_malformed_content(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(cs, "_STATE_FILE", state_file)
        assert cs.read_state() is None

    def test_request_stop_writes_the_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_STATE_FILE", tmp_path / "state.json")
        sentinel = tmp_path / ".context_stop"
        cs.write_state({"pid": 1, "port": 8765, "stop_file": str(sentinel)})
        assert cs.request_stop() is True
        assert sentinel.exists()

    def test_request_stop_without_a_listener_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "_STATE_FILE", tmp_path / "absent.json")
        assert cs.request_stop() is False


# ---------------------------------------------------------------------------
# serve_context_session
# ---------------------------------------------------------------------------

def test_serve_exits_when_the_sentinel_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_STATE_FILE", tmp_path / "state.json")
    session_dir = tmp_path / "session_output"
    sentinel = session_dir / ".context_stop"
    session_dir.mkdir(parents=True)
    sentinel.write_text("stop", encoding="utf-8")

    result = cs.serve_context_session(
        session_dir=session_dir, session_id="unit", port=0,
        token=TOKEN, stop_file=sentinel, poll_interval=0.05,
    )
    assert result == session_dir / cs.CONTEXT_FILENAME
    assert not sentinel.exists()      # consumed on the way out
    assert cs.read_state() is None    # state cleaned up
