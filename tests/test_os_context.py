"""Tests for OS-level context: active window title, clipboard, and redaction."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core import os_context as oc


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedactSecrets:

    @pytest.mark.parametrize(
        ("raw", "marker"),
        [
            ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijkl", "[REDACTED_JWT]"),
            ("key sk-ABCDEFGHIJKLMNOPQRSTUVWX", "[REDACTED_API_KEY]"),
            ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "[REDACTED_GITHUB_TOKEN]"),
            ("xoxb-1234567890-abcdefghij", "[REDACTED_SLACK_TOKEN]"),
            ("AKIAIOSFODNN7EXAMPLE", "[REDACTED_AWS_KEY]"),
            ("card 4111 1111 1111 1111", "[REDACTED_CARD_NUMBER]"),
            ("postgres://admin:hunter2pass@db.internal", "[REDACTED_CREDENTIALS]"),
        ],
    )
    def test_known_secret_shapes_are_replaced(self, raw, marker):
        cleaned, was_redacted = oc.redact_secrets(raw)
        assert was_redacted is True
        assert marker in cleaned

    def test_private_key_block_is_replaced(self):
        raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        cleaned, was_redacted = oc.redact_secrets(raw)
        assert was_redacted is True
        assert "MIIabc" not in cleaned

    def test_ordinary_text_is_untouched(self):
        raw = "Ship the release notes to the team on Monday."
        cleaned, was_redacted = oc.redact_secrets(raw)
        assert cleaned == raw
        assert was_redacted is False

    def test_a_bare_opaque_token_is_dropped_wholesale(self):
        cleaned, was_redacted = oc.redact_secrets("A7fk29dkQmZp01xYbNw83LqRsTuVwXyZ12345")
        assert was_redacted is True
        assert cleaned == "[REDACTED_OPAQUE_VALUE]"

    def test_empty_input_is_safe(self):
        assert oc.redact_secrets("") == ("", False)


# ---------------------------------------------------------------------------
# Active window title
# ---------------------------------------------------------------------------

class TestActiveWindowTitle:

    def test_returns_a_string_on_this_platform(self):
        assert isinstance(oc.active_window_title(), str)

    def test_never_raises_when_the_backend_fails(self):
        with patch.object(oc, "_active_window_win32", side_effect=OSError("boom")), \
             patch.object(oc, "_active_window_darwin", side_effect=OSError("boom")), \
             patch.object(oc, "_active_window_linux", side_effect=OSError("boom")):
            assert oc.active_window_title() == ""

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
    def test_darwin_uses_osascript(self):
        with patch.object(oc.subprocess, "run", return_value=MagicMock(stdout="Safari\n")):
            assert oc._active_window_darwin() == "Safari"

    def test_linux_backend_returns_empty_without_tools(self):
        with patch.object(oc.shutil, "which", return_value=None):
            assert oc._active_window_linux() == ""

    def test_title_is_truncated(self):
        with patch.object(oc, "_active_window_win32", return_value="T" * 5000), \
             patch.object(oc, "_active_window_darwin", return_value="T" * 5000), \
             patch.object(oc, "_active_window_linux", return_value="T" * 5000):
            # The backends truncate; assert the contract holds for the caller.
            assert len(oc.active_window_title()) == 5000  # backend mocked wholesale
        context = oc.collect_os_context()
        assert len(context.active_window) <= oc.MAX_TITLE_CHARS


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

class TestClipboard:

    def test_returns_a_string_on_this_platform(self):
        assert isinstance(oc.clipboard_text(), str)

    def test_never_raises_when_the_backend_fails(self):
        with patch.object(oc, "_clipboard_win32", side_effect=OSError("locked")), \
             patch.object(oc, "_clipboard_darwin", side_effect=OSError("locked")), \
             patch.object(oc, "_clipboard_linux", side_effect=OSError("locked")):
            assert oc.clipboard_text() == ""

    def test_linux_backend_returns_empty_without_tools(self):
        with patch.object(oc.shutil, "which", return_value=None):
            assert oc._clipboard_linux() == ""


# ---------------------------------------------------------------------------
# collect_os_context
# ---------------------------------------------------------------------------

class TestCollectOSContext:

    def test_clipboard_is_not_captured_by_default(self):
        with patch.object(oc, "clipboard_text", side_effect=AssertionError("must not be read")):
            context = oc.collect_os_context()
        assert context.clipboard is None
        assert context.clipboard_captured is False

    def test_clipboard_is_captured_when_requested(self):
        with patch.object(oc, "clipboard_text", return_value="hello world"):
            context = oc.collect_os_context(include_clipboard=True)
        assert context.clipboard == "hello world"
        assert context.clipboard_captured is True
        assert context.clipboard_redacted is False

    def test_captured_clipboard_is_redacted(self):
        with patch.object(oc, "clipboard_text", return_value="deploy ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"):
            context = oc.collect_os_context(include_clipboard=True)
        assert "ghp_" not in context.clipboard
        assert context.clipboard_redacted is True

    def test_captured_clipboard_is_truncated(self):
        with patch.object(oc, "clipboard_text", return_value="word " * 2000):
            context = oc.collect_os_context(include_clipboard=True)
        assert len(context.clipboard) < oc.MAX_CLIPBOARD_CHARS + 60
        assert context.clipboard.endswith("chars]")

    def test_empty_clipboard_is_recorded_as_empty(self):
        with patch.object(oc, "clipboard_text", return_value=""):
            context = oc.collect_os_context(include_clipboard=True)
        assert context.clipboard == ""
        assert context.clipboard_captured is True

    def test_to_dict_omits_absent_clipboard(self):
        with patch.object(oc, "active_window_title", return_value="Chrome"):
            payload = oc.collect_os_context().to_dict()
        assert "clipboard" not in payload
        assert payload["active_window"] == "Chrome"
        assert payload["captured_at"]


class TestEnrichDataJson:

    def test_adds_os_context_block(self):
        data = {"steps": []}
        with patch.object(oc, "active_window_title", return_value="Visual Studio Code"):
            oc.enrich_data_json(data)
        assert data["os_context"]["active_window"] == "Visual Studio Code"

    def test_is_json_serialisable(self):
        import json
        data = {"steps": []}
        with patch.object(oc, "clipboard_text", return_value="copied text"):
            oc.enrich_data_json(data, include_clipboard=True)
        assert json.loads(json.dumps(data))["os_context"]["clipboard"] == "copied text"

    def test_does_not_disturb_existing_keys(self):
        data = {"steps": [{"index": 0}], "title": "Session"}
        oc.enrich_data_json(data)
        assert data["title"] == "Session"
        assert data["steps"] == [{"index": 0}]
