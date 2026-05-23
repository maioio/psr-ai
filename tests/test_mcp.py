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

class TestPsrFetchLink:
    def test_missing_url_returns_error(self, monkeypatch):
        resp = _call(
            _req("tools/call", {"name": "psr_fetch_link", "arguments": {}}),
            monkeypatch,
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_generic_url_returns_text(self, monkeypatch):
        fake_html = b"<html><body><p>Hello world</p></body></html>"

        class _FakeResp:
            def read(self, n):
                return fake_html
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            headers = type("H", (), {"get_content_charset": lambda s: "utf-8"})()

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://example.com"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Hello world" in text
        assert "http://example.com" in text

    def test_max_chars_truncates_output(self, monkeypatch):
        long_body = "X" * 500
        fake_html = f"<html><body><p>{long_body}</p></body></html>".encode()

        class _FakeResp:
            def read(self, n):
                return fake_html
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            headers = type("H", (), {"get_content_charset": lambda s: "utf-8"})()

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
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
        assert len(text) <= 130  # url header + max_chars body

    def test_ytdlp_missing_returns_message(self, monkeypatch):
        import shutil as _shutil
        with patch.object(_shutil, "which", return_value=None):
            resp = _call(
                _req(
                    "tools/call",
                    {"name": "psr_fetch_link", "arguments": {"url": "https://www.youtube.com/watch?v=test"}},
                ),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "yt-dlp" in text

    def test_fetch_failure_returns_message(self, monkeypatch):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            resp = _call(
                _req("tools/call", {"name": "psr_fetch_link", "arguments": {"url": "http://bad.example"}}),
                monkeypatch,
            )

        assert "error" not in resp
        text = resp["result"]["content"][0]["text"]
        assert "Failed to fetch" in text


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
