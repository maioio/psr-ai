"""Tests for the instrumented-browser launcher and the context MCP tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core import context_browser as cb
from psr_ai import mcp


# ---------------------------------------------------------------------------
# Browser discovery
# ---------------------------------------------------------------------------

class TestBrowserDiscovery:

    def test_find_browsers_returns_targets(self):
        for browser in cb.find_browsers():
            assert browser.executable.exists()
            assert browser.name in {"Chrome", "Edge", "Brave"}

    def test_each_browser_gets_its_own_profile(self, tmp_path):
        browsers = cb.find_browsers(profile_dir=tmp_path)
        profiles = [b.profile_dir for b in browsers]
        assert len(profiles) == len(set(profiles)), "profiles must not collide"
        for browser in browsers:
            assert browser.profile_dir.parent == tmp_path

    def test_preferred_browser_is_honoured(self, tmp_path):
        available = cb.find_browsers(profile_dir=tmp_path)
        if not available:
            pytest.skip("no browser installed")
        wanted = available[0].name
        assert cb.find_browser(preferred=wanted, profile_dir=tmp_path).name == wanted

    def test_unknown_browser_returns_none(self, tmp_path):
        assert cb.find_browser(preferred="netscape", profile_dir=tmp_path) is None

    def test_find_browser_defaults_to_first_available(self, tmp_path):
        available = cb.find_browsers(profile_dir=tmp_path)
        expected = available[0].name if available else None
        found = cb.find_browser(profile_dir=tmp_path)
        assert (found.name if found else None) == expected

    def test_extension_dir_contains_the_manifest(self):
        assert (cb.extension_dir() / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# Launch command
# ---------------------------------------------------------------------------

class TestLaunchCommand:

    def _target(self, tmp_path) -> cb.BrowserTarget:
        exe = tmp_path / "msedge.exe"
        exe.touch()
        return cb.BrowserTarget(name="Edge", executable=exe, profile_dir=tmp_path / "profile")

    def test_command_loads_the_extension(self, tmp_path):
        target = self._target(tmp_path)
        ext = tmp_path / "ext"
        args = cb.build_launch_command(target, extension=ext)
        assert args[0] == str(target.executable)
        assert f"--load-extension={ext}" in args

    def test_command_uses_the_dedicated_profile(self, tmp_path):
        target = self._target(tmp_path)
        args = cb.build_launch_command(target, extension=tmp_path / "ext")
        assert f"--user-data-dir={target.profile_dir}" in args

    def test_command_appends_the_url_last(self, tmp_path):
        target = self._target(tmp_path)
        args = cb.build_launch_command(target, url="https://example.com", extension=tmp_path / "ext")
        assert args[-1] == "https://example.com"
        # "--" ends switch parsing, so the URL can never be read as a flag.
        assert args[-2] == "--"

    def test_no_url_means_no_trailing_argument(self, tmp_path):
        target = self._target(tmp_path)
        args = cb.build_launch_command(target, extension=tmp_path / "ext")
        assert not args[-1].startswith("http")
        assert "--" not in args

    def test_command_refuses_a_flag_as_url(self, tmp_path):
        target = self._target(tmp_path)
        with pytest.raises(ValueError):
            cb.build_launch_command(target, url="--gpu-launcher=calc", extension=tmp_path / "ext")

    def test_disable_extensions_except_is_paired_with_load(self, tmp_path):
        """Chromium only honours --load-extension alongside this flag."""
        target = self._target(tmp_path)
        ext = tmp_path / "ext"
        args = cb.build_launch_command(target, extension=ext)
        assert f"--disable-extensions-except={ext}" in args


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

class TestValidateUrl:

    @pytest.mark.parametrize("url", [
        "https://example.com",
        "http://localhost:3000/path?q=1#top",
        "HTTPS://Example.com/",
    ])
    def test_accepts_http_and_https(self, url):
        assert cb.validate_url(url) == url

    @pytest.mark.parametrize("url", [
        "--gpu-launcher=calc",
        "-no-sandbox",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "chrome://settings",
        "data:text/html,hi",
        "http://",
        "example.com",
        "https://example.com/a b",
        'https://example.com/"',
        "https://example.com/\n--gpu-launcher=calc",
        "https://example.com/\x00",
    ])
    def test_rejects_everything_else(self, url):
        with pytest.raises(ValueError):
            cb.validate_url(url)


# ---------------------------------------------------------------------------
# launch()
# ---------------------------------------------------------------------------

class TestLaunch:

    def test_launch_spawns_the_browser_and_creates_the_profile(self, tmp_path):
        exe = tmp_path / "chrome.exe"
        exe.touch()
        ext = tmp_path / "ext"
        ext.mkdir()
        (ext / "manifest.json").write_text("{}", encoding="utf-8")
        target = cb.BrowserTarget("Edge", exe, tmp_path / "profile")

        process = MagicMock()
        process.pid = 4242
        with (
            patch.object(cb, "find_browser", return_value=target),
            patch.object(cb.subprocess, "Popen", return_value=process) as popen,
        ):
            browser, pid = cb.launch(extension=ext)

        assert pid == 4242
        assert browser is target
        assert target.profile_dir.is_dir()
        assert f"--load-extension={ext}" in popen.call_args[0][0]

    def test_chrome_is_refused_because_it_ignores_the_flag(self, tmp_path):
        """Opening Chrome would capture nothing - fail loudly instead."""
        exe = tmp_path / "chrome.exe"
        exe.touch()
        target = cb.BrowserTarget("Chrome", exe, tmp_path / "profile")
        with patch.object(cb, "find_browser", return_value=target):
            with pytest.raises(cb.UnsupportedBrowserError, match="ignores --load-extension"):
                cb.launch()

    def test_chrome_can_be_forced_with_allow_unsupported(self, tmp_path):
        exe = tmp_path / "chrome.exe"
        exe.touch()
        ext = tmp_path / "ext"
        ext.mkdir()
        (ext / "manifest.json").write_text("{}", encoding="utf-8")
        target = cb.BrowserTarget("Chrome", exe, tmp_path / "profile")
        with (
            patch.object(cb, "find_browser", return_value=target),
            patch.object(cb.subprocess, "Popen", return_value=MagicMock(pid=5)),
        ):
            _, pid = cb.launch(extension=ext, allow_unsupported=True)
        assert pid == 5

    def test_auto_load_capable_browsers_are_preferred(self, tmp_path):
        """Edge must outrank Chrome, because only Edge actually loads it."""
        names = [b.name for b in cb.find_browsers(profile_dir=tmp_path)]
        if "Chrome" in names and "Edge" in names:
            assert names.index("Edge") < names.index("Chrome")

    def test_launch_rejects_a_bad_url_before_anything_runs(self):
        with (
            patch.object(cb, "find_browser") as find,
            patch.object(cb.subprocess, "Popen") as popen,
        ):
            with pytest.raises(ValueError):
                cb.launch(url="--gpu-launcher=calc")
        find.assert_not_called()
        popen.assert_not_called()

    def test_launch_without_a_browser_raises(self, tmp_path):
        with patch.object(cb, "find_browser", return_value=None):
            with pytest.raises(FileNotFoundError, match="No supported browser"):
                cb.launch()

    def test_launch_without_extension_files_raises(self, tmp_path):
        exe = tmp_path / "chrome.exe"
        exe.touch()
        target = cb.BrowserTarget("Edge", exe, tmp_path / "profile")
        with patch.object(cb, "find_browser", return_value=target):
            with pytest.raises(FileNotFoundError, match="Extension files not found"):
                cb.launch(extension=tmp_path / "missing")

    @pytest.mark.skipif(sys.platform != "win32", reason="detached flags are Windows-specific")
    def test_browser_is_detached_from_the_cli_process(self, tmp_path):
        exe = tmp_path / "chrome.exe"
        exe.touch()
        ext = tmp_path / "ext"
        ext.mkdir()
        (ext / "manifest.json").write_text("{}", encoding="utf-8")
        target = cb.BrowserTarget("Edge", exe, tmp_path / "profile")

        with (
            patch.object(cb, "find_browser", return_value=target),
            patch.object(cb.subprocess, "Popen", return_value=MagicMock(pid=1)) as popen,
        ):
            cb.launch(extension=ext)

        assert popen.call_args.kwargs["creationflags"] != 0


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def _session_with_events(tmp_path: Path, events: list[dict]) -> Path:
    session_dir = tmp_path / "session_output"
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / "context.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    return session_dir


_FAILURE = {
    "kind": "network_failure", "timestamp": "2026-08-08T10:00:00Z",
    "url": "https://api.example.com/orders", "status": 503, "method": "GET",
}
_CONSOLE = {
    "kind": "console_error", "timestamp": "2026-08-08T10:00:01Z",
    "message": "TypeError: undefined is not a function",
}


class TestMcpContextTools:

    def test_all_context_tools_are_registered(self):
        names = {tool["name"] for tool in mcp._TOOLS}
        assert {"psr_open_context_browser", "psr_get_browser_context",
                "psr_context_status"} <= names

    def test_every_tool_has_a_schema_and_description(self):
        for tool in mcp._TOOLS:
            assert tool["description"].strip()
            assert tool["inputSchema"]["type"] == "object"

    def test_get_browser_context_reads_a_finished_session(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [_FAILURE, _CONSOLE])
        result = mcp._dispatch_tool("psr_get_browser_context", {"session_dir": str(session_dir)})
        payload = json.loads(result)
        assert payload["returned"] == 2
        assert payload["by_kind"]["network_failure"] == 1
        assert payload["events"][0]["status"] == 503

    def test_get_browser_context_filters_by_kind(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [_FAILURE, _CONSOLE])
        result = mcp._dispatch_tool(
            "psr_get_browser_context",
            {"session_dir": str(session_dir), "kind": "console_error"},
        )
        payload = json.loads(result)
        assert payload["returned"] == 1
        assert payload["events"][0]["kind"] == "console_error"

    def test_get_browser_context_honours_limit(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [_FAILURE] * 10)
        result = mcp._dispatch_tool(
            "psr_get_browser_context", {"session_dir": str(session_dir), "limit": 3}
        )
        assert json.loads(result)["returned"] == 3

    def test_get_browser_context_survives_a_bad_limit(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [_FAILURE])
        result = mcp._dispatch_tool(
            "psr_get_browser_context", {"session_dir": str(session_dir), "limit": "abc"}
        )
        assert json.loads(result)["returned"] == 1

    def test_get_browser_context_explains_an_empty_session(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [])
        result = mcp._dispatch_tool("psr_get_browser_context", {"session_dir": str(session_dir)})
        assert "No browser context captured" in result

    def test_get_browser_context_without_a_session(self):
        with patch("psr_ai.core.context_server.read_state", return_value=None):
            result = mcp._dispatch_tool("psr_get_browser_context", {})
        assert "No live recording session" in result

    def test_status_without_a_listener(self):
        with patch("psr_ai.core.context_server.read_state", return_value=None):
            result = mcp._dispatch_tool("psr_context_status", {})
        assert "NOT running" in result
        assert "Browsers available" in result

    def test_status_with_a_live_listener(self, tmp_path):
        session_dir = _session_with_events(tmp_path, [_FAILURE, _CONSOLE])
        state = {"pid": 99, "port": 8765, "session_id": "live", "session_dir": str(session_dir)}
        with patch("psr_ai.core.context_server.read_state", return_value=state):
            result = mcp._dispatch_tool("psr_context_status", {})
        assert "127.0.0.1:8765" in result
        assert "Events captured: 2" in result

    def test_open_context_browser_reports_success(self):
        target = cb.BrowserTarget("Edge", Path("msedge.exe"), Path("profile"))
        with patch("psr_ai.core.context_browser.launch", return_value=(target, 777)):
            result = mcp._dispatch_tool("psr_open_context_browser", {})
        assert "Edge is open" in result
        assert "777" in result

    def test_open_context_browser_explains_a_missing_browser(self):
        with patch("psr_ai.core.context_browser.launch",
                   side_effect=FileNotFoundError("No supported browser found")):
            result = mcp._dispatch_tool("psr_open_context_browser", {})
        assert "chrome://extensions" in result

    def test_open_context_browser_reports_a_launch_failure(self):
        with patch("psr_ai.core.context_browser.launch", side_effect=OSError("denied")):
            result = mcp._dispatch_tool("psr_open_context_browser", {})
        assert "Could not start the browser" in result

    def test_open_context_browser_explains_an_unsupported_browser(self):
        err = cb.UnsupportedBrowserError("Chrome ignores --load-extension")
        with patch("psr_ai.core.context_browser.launch", side_effect=err):
            result = mcp._dispatch_tool("psr_open_context_browser", {"browser": "chrome"})
        assert "Could not open an instrumented browser" in result
        assert "Chrome ignores --load-extension" in result

    def test_open_context_browser_refuses_a_flag_as_url(self):
        """The url comes from an agent; a Chromium switch there must never launch."""
        with patch.object(cb.subprocess, "Popen") as popen:
            result = mcp._dispatch_tool(
                "psr_open_context_browser", {"url": "--gpu-launcher=calc"}
            )
        popen.assert_not_called()
        assert "Only http:// and https://" in result
