"""Tests for liveness detection, the doctor, and the self-test.

The behaviour under test exists to remove one specific ambiguity: a session
with no browser context looks identical whether the page was clean or the
extension was switched off. These tests pin down that distinction.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from psr_ai.cli import main
from psr_ai.core.context_browser import BrowserTarget
from psr_ai.core import context_selftest as st
from psr_ai.core import context_server as cs

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
TOKEN = "liveness-token"
_CLEAN_ENV = {"PSR_AI_VERBOSE": "", "PSR_AI_QUIET": ""}


@pytest.fixture
def server(tmp_path):
    session = cs.ContextSession(session_id="live", session_dir=tmp_path / "session_output")
    store = cs.ContextStore(session)
    instance = cs.ContextServer(store=store, port=0, token=TOKEN)
    instance.start()
    yield instance
    instance.stop()


def _post(url, payload=None, token=TOKEN):
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Origin", EXT_ORIGIN)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        err.read()
        return err.code, {}


def _health(server):
    return cs.probe_listener(port=server.port, token=TOKEN)


# ---------------------------------------------------------------------------
# The hello beacon
# ---------------------------------------------------------------------------

class TestHelloBeacon:

    def test_extension_starts_out_unseen(self, server):
        health = _health(server)
        assert health["extension_seen"] is False
        assert health["extension_live"] is False
        assert health["extension_seen_seconds_ago"] is None

    def test_hello_marks_the_extension_live(self, server):
        status, body = _post(f"http://127.0.0.1:{server.port}/v1/hello",
                             {"client": {"browser": "Chrome", "version": "151.0"}})
        assert status == 200
        assert body["acknowledged"] is True
        health = _health(server)
        assert health["extension_live"] is True
        assert health["client"]["browser"] == "Chrome"

    def test_hello_without_a_body_still_counts(self, server):
        status, _ = _post(f"http://127.0.0.1:{server.port}/v1/hello", None)
        assert status == 200
        assert _health(server)["extension_seen"] is True

    def test_hello_requires_the_token(self, server):
        status, _ = _post(f"http://127.0.0.1:{server.port}/v1/hello", {}, token="wrong")
        assert status == 401
        assert _health(server)["extension_seen"] is False

    def test_ingesting_events_also_counts_as_life(self, server):
        """A busy page should not need a separate heartbeat."""
        _post(f"http://127.0.0.1:{server.port}/v1/ingest",
              {"events": [{"kind": "console_error", "message": "x",
                           "timestamp": "2026-08-08T10:00:00Z"}]})
        assert _health(server)["extension_live"] is True

    def test_client_fields_are_bounded(self, server):
        _post(f"http://127.0.0.1:{server.port}/v1/hello",
              {"client": {"browser": "C" * 500, **{f"k{i}": i for i in range(50)}}})
        client = _health(server)["client"]
        assert len(client) <= 10
        assert len(client["browser"]) <= 80

    def test_hello_is_not_written_into_the_event_log(self, server):
        _post(f"http://127.0.0.1:{server.port}/v1/hello", {"client": {"browser": "Edge"}})
        assert cs.load_context_events(server.store.session.session_dir) == []


# ---------------------------------------------------------------------------
# Liveness persistence
# ---------------------------------------------------------------------------

class TestLivenessPersistence:

    def test_meta_file_records_the_client(self, tmp_path):
        session = cs.ContextSession(session_id="s", session_dir=tmp_path / "out")
        store = cs.ContextStore(session)
        store.mark_seen({"browser": "Chrome"})
        store.flush_meta()
        meta = cs.read_context_meta(tmp_path / "out")
        assert meta["client"]["browser"] == "Chrome"
        assert meta["last_seen"]

    def test_read_meta_is_none_when_absent(self, tmp_path):
        assert cs.read_context_meta(tmp_path) is None

    def test_read_meta_survives_corruption(self, tmp_path):
        (tmp_path / cs.META_FILENAME).write_text("{not json", encoding="utf-8")
        assert cs.read_context_meta(tmp_path) is None

    def test_mark_seen_without_a_session_is_false(self):
        assert cs.ContextStore(None).mark_seen({"browser": "Chrome"}) is False

    def test_seconds_since_seen_is_none_before_contact(self, tmp_path):
        session = cs.ContextSession(session_id="s", session_dir=tmp_path)
        assert session.seconds_since_seen() is None


# ---------------------------------------------------------------------------
# probe_listener
# ---------------------------------------------------------------------------

class TestProbeListener:

    def test_returns_health_from_a_live_listener(self, server):
        assert cs.probe_listener(port=server.port, token=TOKEN)["recording"] is True

    def test_returns_none_when_nothing_listens(self):
        assert cs.probe_listener(port=1) is None

    def test_reports_an_auth_failure_distinctly(self, server):
        assert cs.probe_listener(port=server.port, token="wrong")["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# sa context doctor
# ---------------------------------------------------------------------------

def _invoke(args):
    return CliRunner().invoke(main, args, env=dict(_CLEAN_ENV))


class TestDoctor:

    def test_reports_a_missing_listener_and_exits_nonzero(self):
        with (
            patch("psr_ai.core.context_server.read_state", return_value=None),
            patch("psr_ai.core.context_server.probe_listener", return_value=None),
        ):
            result = _invoke(["context", "doctor"])
        assert result.exit_code == 1
        assert "Listener" in result.stdout and "DOWN" in result.stdout
        assert "sa start" in result.stdout

    def test_reports_a_disabled_extension_with_a_fix(self, tmp_path):
        state = {"pid": 1, "port": 8765, "session_id": "s", "session_dir": str(tmp_path)}
        health = {"recording": True, "session_id": "s", "extension_seen": False,
                  "extension_live": False, "extension_seen_seconds_ago": None, "client": {}}
        with (
            patch("psr_ai.core.context_server.read_state", return_value=state),
            patch("psr_ai.core.context_server.probe_listener", return_value=health),
        ):
            result = _invoke(["context", "doctor"])
        assert result.exit_code == 1
        assert "Extension" in result.stdout and "DOWN" in result.stdout
        assert "chrome://extensions" in result.stdout

    def test_reports_a_stale_extension(self, tmp_path):
        state = {"pid": 1, "port": 8765, "session_id": "s", "session_dir": str(tmp_path)}
        health = {"recording": True, "session_id": "s", "extension_seen": True,
                  "extension_live": False, "extension_seen_seconds_ago": 400.0,
                  "client": {"browser": "Chrome", "version": "151"}}
        with (
            patch("psr_ai.core.context_server.read_state", return_value=state),
            patch("psr_ai.core.context_server.probe_listener", return_value=health),
        ):
            result = _invoke(["context", "doctor"])
        assert result.exit_code == 1
        assert "STALE" in result.stdout

    def test_all_green_exits_zero(self, tmp_path):
        state = {"pid": 1, "port": 8765, "session_id": "s", "session_dir": str(tmp_path)}
        health = {"recording": True, "session_id": "s", "extension_seen": True,
                  "extension_live": True, "extension_seen_seconds_ago": 3.0,
                  "client": {"browser": "Chrome", "version": "151"}}
        # CI runners have no Chromium installed, and "no browser" is a problem.
        edge = BrowserTarget("Edge", Path("msedge.exe"), Path("profile"))
        with (
            patch("psr_ai.core.context_server.read_state", return_value=state),
            patch("psr_ai.core.context_server.probe_listener", return_value=health),
            patch("psr_ai.core.context_browser.find_browsers", return_value=[edge]),
        ):
            result = _invoke(["context", "doctor"])
        assert result.exit_code == 0
        assert "Context layer is working." in result.stdout
        assert "Chrome 151" in result.stdout

    def test_reports_a_rejected_token(self, tmp_path):
        state = {"pid": 1, "port": 8765, "session_id": "s", "session_dir": str(tmp_path)}
        with (
            patch("psr_ai.core.context_server.read_state", return_value=state),
            patch("psr_ai.core.context_server.probe_listener",
                  return_value={"error": "unauthorized"}),
        ):
            result = _invoke(["context", "doctor"])
        assert result.exit_code == 1
        assert "sa context token" in result.stdout


# ---------------------------------------------------------------------------
# sa context test / self-test engine
# ---------------------------------------------------------------------------

class TestSelfTest:

    def _events(self) -> list[dict]:
        return [
            {"kind": "network_failure", "status": 500,
             "url": f"http://127.0.0.1/api/{st.MARKER}",
             "response_body": f'{{"error":"{st.MARKER} deliberate failure"}}'},
            {"kind": "console_error", "message": f"{st.MARKER}: deliberate console error"},
        ]

    def test_all_signals_present_passes(self):
        checks = st._evaluate(self._events())
        assert all(checks.values())
        assert st.SelfTestResult(checks=checks).passed is True

    def test_missing_console_error_fails_with_a_hint(self):
        events = [e for e in self._events() if e["kind"] != "console_error"]
        checks = st._evaluate(events)
        assert checks["console error captured"] is False
        assert any("console error was missed" in h for h in st._hints_for(checks))

    def test_nothing_captured_points_at_the_extension(self):
        checks = st._evaluate([])
        hints = st._hints_for(checks)
        assert any("sa context doctor" in h for h in hints)

    def test_unrelated_events_do_not_satisfy_the_checks(self):
        """A previous session's noise must not make a broken layer look fine."""
        checks = st._evaluate([
            {"kind": "network_failure", "status": 500, "url": "https://example.com/other"},
            {"kind": "console_error", "message": "some unrelated error"},
        ])
        assert not any(checks.values())

    def test_score_is_reported(self):
        result = st.SelfTestResult(checks={"a": True, "b": False})
        assert result.score == "1/2 signals"

    def test_run_self_test_only_counts_new_events(self, tmp_path):
        session_dir = tmp_path / "out"
        session_dir.mkdir()
        # Pre-existing events that would satisfy every check if counted.
        with (session_dir / cs.CONTEXT_FILENAME).open("w", encoding="utf-8") as handle:
            for event in self._events():
                handle.write(json.dumps(event) + "\n")

        result = st.run_self_test(session_dir, timeout=1.0, port=8798, open_browser=False)
        assert not result.passed, "stale events must not count as a pass"

    def test_cli_requires_an_active_session(self):
        with patch("psr_ai.core.context_server.read_state", return_value=None):
            result = _invoke(["context", "test"])
        assert result.exit_code == 1
        assert "sa start" in result.stderr


# ---------------------------------------------------------------------------
# sa stop warning
# ---------------------------------------------------------------------------

class TestStopWarning:

    def test_warns_when_the_layer_was_never_seen(self, tmp_path, capsys):
        from psr_ai.cli import _warn_if_layer_never_seen
        _warn_if_layer_never_seen(tmp_path, merged=0)
        assert "never checked in" in capsys.readouterr().err

    def test_stays_quiet_when_the_layer_was_live(self, tmp_path, capsys):
        from psr_ai.cli import _warn_if_layer_never_seen
        (tmp_path / cs.META_FILENAME).write_text(
            json.dumps({"last_seen": 1.0, "client": {"browser": "Chrome"}}), encoding="utf-8")
        _warn_if_layer_never_seen(tmp_path, merged=0)
        assert capsys.readouterr().err == ""

    def test_stays_quiet_when_context_was_captured(self, tmp_path, capsys):
        from psr_ai.cli import _warn_if_layer_never_seen
        _warn_if_layer_never_seen(tmp_path, merged=5)
        assert capsys.readouterr().err == ""
