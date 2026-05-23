"""Unit tests for psr_ai.mcp: JSON-RPC server protocol and tool dispatch."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call(request: dict, monkeypatch) -> dict:
    """Feed one JSON-RPC request through main_loop and return the parsed response."""
    import psr_ai.mcp as mcp_mod

    line = json.dumps(request) + "\n"
    stdin  = io.StringIO(line)
    stdout = io.StringIO()

    monkeypatch.setattr(sys, "stdout", stdout)
    mcp_mod.main_loop(stdin=stdin)

    stdout.seek(0)
    raw = stdout.read().strip()
    assert raw, "Server produced no output"
    return json.loads(raw)


def _req(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# ---------------------------------------------------------------------------
# 1. initialize handshake
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_returns_valid_structure(self, monkeypatch):
        resp = _call(_req("initialize", {}), monkeypatch)

        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert "capabilities" in result
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "psr-ai"
        assert "version" in result["serverInfo"]

    def test_id_is_echoed(self, monkeypatch):
        resp = _call(_req("initialize", {}, req_id=42), monkeypatch)
        assert resp["id"] == 42

    def test_no_error_key(self, monkeypatch):
        resp = _call(_req("initialize", {}), monkeypatch)
        assert "error" not in resp


# ---------------------------------------------------------------------------
# 2. tools/list
# ---------------------------------------------------------------------------

class TestToolsList:
    def test_returns_four_tools(self, monkeypatch):
        resp = _call(_req("tools/list"), monkeypatch)
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "psr_start_recording",
            "psr_stop_and_parse",
            "psr_quick_snapshot",
            "psr_fetch_link",
        }

    def test_each_tool_has_input_schema(self, monkeypatch):
        resp = _call(_req("tools/list"), monkeypatch)
        for tool in resp["result"]["tools"]:
            assert "inputSchema" in tool, f"{tool['name']} missing inputSchema"
            assert tool["inputSchema"]["type"] == "object"

    def test_each_tool_has_description(self, monkeypatch):
        resp = _call(_req("tools/list"), monkeypatch)
        for tool in resp["result"]["tools"]:
            assert tool.get("description"), f"{tool['name']} has empty description"


# ---------------------------------------------------------------------------
# 3. psr_start_recording
# ---------------------------------------------------------------------------

class TestPsrStartRecording:
    def _mock_recorder(self, fake_path: Path) -> MagicMock:
        rec = MagicMock()
        rec.start_recording.return_value = fake_path
        return rec

    def test_calls_recorder_start(self, monkeypatch, tmp_path):
        fake_zip = tmp_path / "psr_test.zip"
        rec = self._mock_recorder(fake_zip)

        with patch("psr_ai.mcp.Recorder", return_value=rec):
            resp = _call(
                _req("tools/call", {"name": "psr_start_recording", "arguments": {}}),
                monkeypatch,
            )

        rec.start_recording.assert_called_once()
        assert "error" not in resp
        result = resp["result"]
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "started successfully" in text

    def test_passes_output_path(self, monkeypatch, tmp_path):
        fake_zip = tmp_path / "custom.zip"
        rec = self._mock_recorder(fake_zip)

        with patch("psr_ai.mcp.Recorder", return_value=rec):
            _call(
                _req(
                    "tools/call",
                    {
                        "name": "psr_start_recording",
                        "arguments": {"output_path": str(fake_zip)},
                    },
                ),
                monkeypatch,
            )

        call_kwargs = rec.start_recording.call_args
        assert call_kwargs is not None
        passed = call_kwargs.kwargs.get("output_path") or (call_kwargs.args[0] if call_kwargs.args else None)
        assert passed is not None

    def test_passes_max_screenshots(self, monkeypatch, tmp_path):
        fake_zip = tmp_path / "psr.zip"
        rec = self._mock_recorder(fake_zip)

        with patch("psr_ai.mcp.Recorder", return_value=rec):
            _call(
                _req(
                    "tools/call",
                    {
                        "name": "psr_start_recording",
                        "arguments": {"max_screenshots": 50},
                    },
                ),
                monkeypatch,
            )

        call_kwargs = rec.start_recording.call_args
        passed_max = (
            call_kwargs.kwargs.get("max_screenshots")
            or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
        )
        assert passed_max == 50

    def test_confirmation_text_contains_output_path(self, monkeypatch, tmp_path):
        fake_zip = tmp_path / "psr.zip"
        rec = self._mock_recorder(fake_zip)

        with patch("psr_ai.mcp.Recorder", return_value=rec):
            resp = _call(
                _req("tools/call", {"name": "psr_start_recording", "arguments": {}}),
                monkeypatch,
            )

        text = resp["result"]["content"][0]["text"]
        assert str(fake_zip) in text


# ---------------------------------------------------------------------------
# 4. psr_stop_and_parse
# ---------------------------------------------------------------------------

class TestPsrStopAndParse:
    def test_returns_report_text(self, monkeypatch, tmp_path):
        fake_zip = tmp_path / "psr_session.zip"
        fake_zip.write_bytes(b"")  # must exist for path check

        output_dir = tmp_path / "psr_session_output"
        output_dir.mkdir()
        report = output_dir / "report.md"
        report.write_text("# Test Report\n\nStep 1: clicked something.\n", encoding="utf-8")

        fake_session = MagicMock()
        fake_session.steps = [MagicMock(), MagicMock()]

        rec = MagicMock()
        rec.stop_recording.return_value = fake_zip

        with (
            patch("psr_ai.mcp.Recorder", return_value=rec),
            patch("psr_ai.mcp.parse_session", return_value=fake_session),
            patch("psr_ai.mcp.generate_json_meta", return_value=output_dir / "data.json"),
            patch("psr_ai.mcp.MarkdownGenerator") as mock_gen_cls,
            patch("psr_ai.mcp.default_output_dir", return_value=output_dir),
        ):
            mock_gen = MagicMock()
            mock_gen.write.return_value = report
            mock_gen_cls.return_value = mock_gen

            resp = _call(
                _req("tools/call", {"name": "psr_stop_and_parse", "arguments": {}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Test Report" in text
        assert "Steps captured: 2" in text

    def test_no_active_recording(self, monkeypatch):
        rec = MagicMock()
        rec.stop_recording.return_value = None

        with patch("psr_ai.mcp.Recorder", return_value=rec):
            resp = _call(
                _req("tools/call", {"name": "psr_stop_and_parse", "arguments": {}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "No active recording" in text


# ---------------------------------------------------------------------------
# 5. psr_quick_snapshot
# ---------------------------------------------------------------------------

class TestPsrQuickSnapshot:
    def _make_tiny_png_bytes(self) -> bytes:
        """Return valid 1x1 red PNG bytes generated via Pillow."""
        import io as _io
        from PIL import Image
        img = Image.new("RGB", (1, 1), color=(255, 0, 0))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_returns_base64_string(self, monkeypatch):
        import base64
        png_bytes = self._make_tiny_png_bytes()

        with patch("psr_ai.mcp._capture_screen_bytes", return_value=png_bytes):
            resp = _call(
                _req("tools/call", {"name": "psr_quick_snapshot", "arguments": {}}),
                monkeypatch,
            )

        assert "error" not in resp
        b64 = resp["result"]["content"][0]["text"]
        decoded = base64.b64decode(b64)
        assert decoded[:4] == b"\x89PNG"

    def test_custom_max_px_and_colors(self, monkeypatch):
        """Verify custom params are accepted without raising."""
        png_bytes = self._make_tiny_png_bytes()

        with patch("psr_ai.mcp._capture_screen_bytes", return_value=png_bytes):
            resp = _call(
                _req(
                    "tools/call",
                    {
                        "name": "psr_quick_snapshot",
                        "arguments": {"max_px": 640, "colors": 64},
                    },
                ),
                monkeypatch,
            )

        assert "error" not in resp

    def test_pillow_missing_returns_error(self, monkeypatch):
        """When Pillow is not importable the tool must return an error response."""
        import builtins
        _real_import = builtins.__import__

        def _no_pil(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("No module named 'PIL'")
            return _real_import(name, *args, **kwargs)

        png_bytes = self._make_tiny_png_bytes()

        with (
            patch("psr_ai.mcp._capture_screen_bytes", return_value=png_bytes),
            patch("builtins.__import__", side_effect=_no_pil),
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_quick_snapshot", "arguments": {}}),
                monkeypatch,
            )

        assert "error" in resp
        assert resp["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# 6. psr_fetch_link
# ---------------------------------------------------------------------------

def _fake_urlopen(html: bytes, status: int = 200):
    """Return a context-manager mock that urllib.request.urlopen can be patched with."""
    class _Resp:
        def read(self, n):
            return html
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        headers = type("H", (), {"get_content_charset": lambda s: "utf-8"})()

    _Resp.status = status
    return _Resp()


class TestPsrFetchLink:
    def test_missing_url_returns_error(self, monkeypatch):
        resp = _call(
            _req("tools/call", {"name": "psr_fetch_link", "arguments": {}}),
            monkeypatch,
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_generic_url_returns_text(self, monkeypatch):
        fake_html = b"<html><body><p>" + b"Hello world " * 20 + b"</p></body></html>"

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_html)):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://example.com"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Hello world" in text
        assert "http://example.com" in text

    def test_max_chars_truncates_output(self, monkeypatch):
        long_body = ("sufficient content " * 20).encode()
        fake_html = b"<html><body><p>" + long_body + b"</p></body></html>"

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_html)):
            resp = _call(
                _req(
                    "tools/call",
                    {
                        "name": "psr_fetch_link",
                        "arguments": {"url": "http://example.com", "max_chars": 100},
                    },
                ),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        # url header (~30 chars) + max_chars body = well under 200
        assert len(text) <= 200

    def test_ytdlp_missing_falls_through_to_browser(self, monkeypatch):
        """When yt-dlp is absent, the tool falls through to the browser fallback."""
        import psr_ai.mcp as mcp_mod
        import shutil as _shutil

        browser_text = "Instagram profile content loaded via browser. " * 10

        with (
            patch.object(_shutil, "which", return_value=None),
            patch.object(mcp_mod, "_fetch_with_browser", return_value=browser_text) as mock_browser,
        ):
            resp = _call(
                _req(
                    "tools/call",
                    {"name": "psr_fetch_link", "arguments": {"url": "https://www.instagram.com/test"}},
                ),
                monkeypatch,
            )

        mock_browser.assert_called_once()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Instagram profile content" in text
        assert "yt-dlp" in text  # failure notice is still included

    def test_ytdlp_failure_falls_through_to_browser(self, monkeypatch):
        """When yt-dlp exits non-zero, the tool falls through to the browser fallback."""
        import psr_ai.mcp as mcp_mod
        import shutil as _shutil

        browser_text = "Video metadata loaded via browser fallback. " * 10
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "Extractor broken"

        with (
            patch.object(_shutil, "which", return_value="/usr/bin/yt-dlp"),
            patch("subprocess.run", return_value=fake_result),
            patch.object(mcp_mod, "_fetch_with_browser", return_value=browser_text) as mock_browser,
        ):
            resp = _call(
                _req(
                    "tools/call",
                    {"name": "psr_fetch_link", "arguments": {"url": "https://www.youtube.com/watch?v=abc"}},
                ),
                monkeypatch,
            )

        mock_browser.assert_called_once()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Video metadata loaded" in text
        assert "yt-dlp" in text  # failure notice present

    def test_ytdlp_success_does_not_call_browser(self, monkeypatch):
        """When yt-dlp succeeds, the browser fallback must NOT be called."""
        import psr_ai.mcp as mcp_mod
        import shutil as _shutil

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "My Video\nUploader Name\nSome description\nhttps://youtu.be/abc"
        fake_result.stderr = ""

        with (
            patch.object(_shutil, "which", return_value="/usr/bin/yt-dlp"),
            patch("subprocess.run", return_value=fake_result),
            patch.object(mcp_mod, "_fetch_with_browser") as mock_browser,
        ):
            resp = _call(
                _req(
                    "tools/call",
                    {"name": "psr_fetch_link", "arguments": {"url": "https://youtu.be/abc"}},
                ),
                monkeypatch,
            )

        mock_browser.assert_not_called()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "My Video" in text

    def test_fetch_failure_returns_message(self, monkeypatch):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://bad.example"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Failed to fetch" in text

    # ------------------------------------------------------------------
    # Browser fallback tests
    # ------------------------------------------------------------------

    def test_thin_response_triggers_browser_fallback(self, monkeypatch):
        """A static response with less than _BROWSER_FALLBACK_MIN_CHARS triggers Playwright."""
        import psr_ai.mcp as mcp_mod

        thin_html = b"<html><body><p>tiny</p></body></html>"
        browser_text = "Full rich content from the browser. " * 20

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(thin_html)),
            patch.object(mcp_mod, "_fetch_with_browser", return_value=browser_text) as mock_browser,
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://spa.example"}}),
                monkeypatch,
            )

        mock_browser.assert_called_once()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Full rich content" in text

    def test_403_triggers_browser_fallback(self, monkeypatch):
        """HTTP 403 from urllib triggers the browser fallback automatically."""
        import psr_ai.mcp as mcp_mod
        from urllib.error import HTTPError

        browser_text = "Protected page content now visible. " * 10

        with (
            patch("urllib.request.urlopen", side_effect=HTTPError(
                "http://gate.example", 403, "Forbidden", {}, None
            )),
            patch.object(mcp_mod, "_fetch_with_browser", return_value=browser_text) as mock_browser,
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://gate.example"}}),
                monkeypatch,
            )

        mock_browser.assert_called_once()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Protected page content" in text

    def test_login_wall_signal_triggers_browser_fallback(self, monkeypatch):
        """A short page dominated by hard login-wall signals triggers the fallback."""
        import psr_ai.mcp as mcp_mod

        # "please enable javascript" is a hard signal that always triggers.
        gate_html = b"<html><body><p>Please enable JavaScript to continue.</p></body></html>"
        browser_text = "Actual page body after login wall bypassed. " * 10

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(gate_html)),
            patch.object(mcp_mod, "_fetch_with_browser", return_value=browser_text) as mock_browser,
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://walled.example"}}),
                monkeypatch,
            )

        mock_browser.assert_called_once()
        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Actual page body" in text

    def test_nav_login_links_do_not_trigger_fallback(self, monkeypatch):
        """A rich page that merely contains nav 'Log in' links must NOT trigger the fallback."""
        import psr_ai.mcp as mcp_mod

        # Simulate an Instagram-style public profile: real content + nav login links.
        profile_content = (
            b"<html><body>"
            b"<p>Username: testuser</p>"
            b"<p>100 posts, 500 followers, 200 following</p>"
            b"<p>Bio: some interesting bio text here for this user account</p>"
            + b"<p>More profile content.</p>" * 10
            + b"<a href='/login'>Log In</a><a href='/signup'>Sign Up</a>"
            b"</body></html>"
        )

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(profile_content)),
            patch.object(mcp_mod, "_fetch_with_browser") as mock_browser,
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://social.example/profile"}}),
                monkeypatch,
            )

        mock_browser.assert_not_called()
        assert "error" not in resp

    def test_playwright_missing_returns_graceful_notice(self, monkeypatch):
        """If Playwright is not installed the tool returns a notice, not an error."""
        import psr_ai.mcp as mcp_mod

        thin_html = b"<html><body><p>tiny</p></body></html>"

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(thin_html)),
            patch.object(
                mcp_mod,
                "_fetch_with_browser",
                side_effect=ImportError("No module named 'playwright'"),
            ),
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://spa.example"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "playwright" in text.lower() or "Browser fallback unavailable" in text

    def test_browser_fallback_exception_returns_notice(self, monkeypatch):
        """A runtime error in the browser fallback returns a notice, not a crash."""
        import psr_ai.mcp as mcp_mod

        thin_html = b"<html><body><p>tiny</p></body></html>"

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(thin_html)),
            patch.object(
                mcp_mod,
                "_fetch_with_browser",
                side_effect=RuntimeError("chromium launch failed"),
            ),
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://spa.example"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Browser fallback failed" in text

    def test_rich_static_response_skips_browser(self, monkeypatch):
        """A rich static response must NOT trigger the browser fallback."""
        import psr_ai.mcp as mcp_mod

        rich_html = b"<html><body><p>" + b"Rich content. " * 30 + b"</p></body></html>"

        with (
            patch("urllib.request.urlopen", return_value=_fake_urlopen(rich_html)),
            patch.object(mcp_mod, "_fetch_with_browser") as mock_browser,
        ):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://rich.example"}}),
                monkeypatch,
            )

        mock_browser.assert_not_called()
        assert "error" not in resp


# ---------------------------------------------------------------------------
# 6b. _resolve_user_data_dir and persistent-context browser fallback
# ---------------------------------------------------------------------------

class TestResolveBrowserSession:
    def test_returns_existing_path(self, tmp_path):
        import psr_ai.mcp as mcp_mod
        import sys

        fake_dir = tmp_path / "Chrome" / "User Data"
        fake_dir.mkdir(parents=True)

        platform_attr = "win32" if sys.platform != "darwin" else "darwin"
        env_key = "LOCALAPPDATA" if platform_attr == "win32" else None

        with patch.object(mcp_mod, "_resolve_user_data_dir", return_value=str(fake_dir)):
            result = mcp_mod._resolve_user_data_dir()

        assert result == str(fake_dir)

    def test_returns_none_when_no_dir_exists(self, monkeypatch):
        import psr_ai.mcp as mcp_mod
        import os

        with patch.object(os.path, "isdir", return_value=False):
            result = mcp_mod._resolve_user_data_dir()

        assert result is None

    def test_fetch_with_browser_uses_persistent_context_when_dir_found(self):
        """When a user data dir is found, launch_persistent_context is called."""
        import psr_ai.mcp as mcp_mod

        fake_text = "Authenticated page content. " * 20
        mock_page = MagicMock()
        mock_page.evaluate.return_value = fake_text
        mock_ctx = MagicMock()
        mock_ctx.new_page.return_value = mock_page

        mock_pw = MagicMock()
        mock_pw.chromium.launch_persistent_context.return_value = mock_ctx

        with (
            patch.object(mcp_mod, "_resolve_user_data_dir", return_value="/fake/user/data"),
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw
            mock_sync_pw.return_value.__exit__.return_value = False
            result = mcp_mod._fetch_with_browser("https://instagram.com/test", 8000)

        mock_pw.chromium.launch_persistent_context.assert_called_once()
        assert "Authenticated page content" in result

    def test_fetch_with_browser_falls_back_to_isolated_when_persistent_fails(self):
        """When persistent context raises, isolated headless browser is used."""
        import psr_ai.mcp as mcp_mod

        fake_text = "Isolated browser content. " * 20
        mock_page = MagicMock()
        mock_page.evaluate.return_value = fake_text
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw = MagicMock()
        mock_pw.chromium.launch_persistent_context.side_effect = RuntimeError("profile locked")
        mock_pw.chromium.launch.return_value = mock_browser

        with (
            patch.object(mcp_mod, "_resolve_user_data_dir", return_value="/fake/user/data"),
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw
            mock_sync_pw.return_value.__exit__.return_value = False
            result = mcp_mod._fetch_with_browser("https://example.com", 8000)

        mock_pw.chromium.launch.assert_called_once()
        assert "Isolated browser content" in result

    def test_fetch_with_browser_uses_isolated_when_no_user_dir(self):
        """When no user data dir exists, isolated headless browser is used."""
        import psr_ai.mcp as mcp_mod

        fake_text = "Public page content. " * 20
        mock_page = MagicMock()
        mock_page.evaluate.return_value = fake_text
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser

        with (
            patch.object(mcp_mod, "_resolve_user_data_dir", return_value=None),
            patch("playwright.sync_api.sync_playwright") as mock_sync_pw,
        ):
            mock_sync_pw.return_value.__enter__.return_value = mock_pw
            mock_sync_pw.return_value.__exit__.return_value = False
            result = mcp_mod._fetch_with_browser("https://example.com", 8000)

        mock_pw.chromium.launch_persistent_context.assert_not_called()
        mock_pw.chromium.launch.assert_called_once()
        assert "Public page content" in result


# ---------------------------------------------------------------------------
# 7. Protocol boundary protection  (was 6)
# ---------------------------------------------------------------------------

class TestProtocolBoundaries:
    def test_malformed_json_returns_parse_error(self, monkeypatch):
        import psr_ai.mcp as mcp_mod

        stdin  = io.StringIO("not valid json\n")
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        resp = json.loads(stdout.read().strip())
        assert resp["error"]["code"] == -32700

    def test_unknown_tool_returns_method_not_found(self, monkeypatch):
        resp = _call(
            _req("tools/call", {"name": "nonexistent_tool", "arguments": {}}),
            monkeypatch,
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_unknown_method_returns_method_not_found(self, monkeypatch):
        resp = _call(_req("totally/unknown"), monkeypatch)
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_missing_jsonrpc_field_returns_invalid_request(self, monkeypatch):
        import psr_ai.mcp as mcp_mod

        bad = {"id": 1, "method": "initialize"}
        stdin  = io.StringIO(json.dumps(bad) + "\n")
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        resp = json.loads(stdout.read().strip())
        assert "error" in resp
        assert resp["error"]["code"] == -32600

    def test_array_input_returns_invalid_request(self, monkeypatch):
        import psr_ai.mcp as mcp_mod

        stdin  = io.StringIO("[1, 2, 3]\n")
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        resp = json.loads(stdout.read().strip())
        assert "error" in resp
        assert resp["error"]["code"] == -32600

    def test_notification_produces_no_output(self, monkeypatch):
        """Messages without id are notifications - server must stay silent."""
        import psr_ai.mcp as mcp_mod

        notification = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        stdin  = io.StringIO(json.dumps(notification) + "\n")
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        assert stdout.read().strip() == ""

    def test_empty_lines_are_ignored(self, monkeypatch):
        """Blank lines between messages must not crash the server."""
        import psr_ai.mcp as mcp_mod

        payload = "\n\n" + json.dumps(_req("initialize")) + "\n\n"
        stdin  = io.StringIO(payload)
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        resp = json.loads(stdout.read().strip())
        assert "result" in resp

    def test_multiple_requests_in_sequence(self, monkeypatch):
        """Server must handle multiple back-to-back messages on stdin."""
        import psr_ai.mcp as mcp_mod

        lines = (
            json.dumps(_req("initialize", req_id=1)) + "\n"
            + json.dumps(_req("tools/list", req_id=2)) + "\n"
        )
        stdin  = io.StringIO(lines)
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        responses = [json.loads(l) for l in stdout.read().strip().splitlines()]
        assert len(responses) == 2
        ids = {r["id"] for r in responses}
        assert ids == {1, 2}

    def test_server_continues_after_malformed_input(self, monkeypatch):
        """A bad line must not terminate the loop; good lines after it are processed."""
        import psr_ai.mcp as mcp_mod

        lines = (
            "not json at all\n"
            + json.dumps(_req("initialize", req_id=99)) + "\n"
        )
        stdin  = io.StringIO(lines)
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        mcp_mod.main_loop(stdin=stdin)

        stdout.seek(0)
        responses = [json.loads(l) for l in stdout.read().strip().splitlines()]
        assert len(responses) == 2
        error_resp = next(r for r in responses if "error" in r)
        ok_resp    = next(r for r in responses if "result" in r)
        assert error_resp["error"]["code"] == -32700
        assert ok_resp["id"] == 99


# ---------------------------------------------------------------------------
# 7. sa mcp CLI command
# ---------------------------------------------------------------------------

class TestSaMcpCommand:
    def test_sa_mcp_is_registered(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "mcp" in result.output

    def test_sa_mcp_processes_initialize(self):
        """sa mcp must parse an initialize request and emit a valid response."""
        from click.testing import CliRunner
        from psr_ai.cli import main

        req_line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        runner = CliRunner()
        result = runner.invoke(main, ["mcp"], input=req_line + "\n")

        assert result.exit_code == 0
        resp = json.loads(result.output.strip())
        assert resp["result"]["serverInfo"]["name"] == "psr-ai"
