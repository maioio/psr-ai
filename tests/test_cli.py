"""CLI integration tests for the `sa` command group via click.testing.CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from psr_ai.cli import main


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_session_dir(tmp_path: Path, n_steps: int = 2) -> Path:
    """Create a minimal session directory with data.json and dummy screenshots."""
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()
    _TINY_PNG = bytes([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41,
        0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
        0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc,
        0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
        0x44, 0xae, 0x42, 0x60, 0x82,
    ])
    steps = []
    for i in range(n_steps):
        png = screenshots_dir / f"step_{i:03d}.png"
        png.write_bytes(_TINY_PNG)
        steps.append({
            "index": i,
            "label": f"Step {i}",
            "timestamp": "1/15/2024 2:30:00 PM",
            "program": "TestApp",
            "action_text": f"Clicked button {i}",
            "screenshot": f"screenshots/step_{i:03d}.png",
        })

    data = {
        "title": "Test Session",
        "source": "test.zip",
        "generated_at": "2024-01-15T14:30:00Z",
        "step_count": n_steps,
        "steps": steps,
    }
    (tmp_path / "data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------
# sa --version
# ------------------------------------------------------------------

def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "sa" in result.output


# ------------------------------------------------------------------
# sa status
# ------------------------------------------------------------------

def _make_fake_recorder(psr_path: Path):
    """Return a WindowsRecorder-like fake that bypasses all resolution logic."""
    from psr_ai.core.recorder import WindowsRecorder
    r = object.__new__(WindowsRecorder)
    r._psr_exe = psr_path
    r._pending_output = None
    return r


def test_status_idle(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    runner = CliRunner()
    mock_result = MagicMock()
    mock_result.stdout = "INFO: No tasks are running."
    with (
        patch("psr_ai.cli.Recorder", return_value=_make_fake_recorder(fake_psr)),
        patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result),
    ):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "IDLE" in result.output


def test_status_recording(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    runner = CliRunner()
    mock_result = MagicMock()
    mock_result.stdout = "psr.exe  1234  Console  1  10,000 K"
    with (
        patch("psr_ai.cli.Recorder", return_value=_make_fake_recorder(fake_psr)),
        patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result),
    ):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "RECORDING" in result.output


def test_status_exits_1_when_psr_not_found():
    from psr_ai.core.recorder import PSRNotFoundError
    runner = CliRunner()
    with patch("psr_ai.cli.Recorder", side_effect=PSRNotFoundError("not found")):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 1


# ------------------------------------------------------------------
# sa review
# ------------------------------------------------------------------

def test_review_keep_all_steps(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=2)
    runner = CliRunner()

    # Simulate pressing Enter (keep) for every step.
    result = runner.invoke(main, ["review", str(source_dir)], input="\n\n")

    assert result.exit_code == 0
    assert "2 steps kept" in result.output
    assert "0 steps dropped" in result.output


def test_review_drop_one_step(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=2)
    runner = CliRunner()

    # Drop the first step, keep the second.
    result = runner.invoke(main, ["review", str(source_dir)], input="d\n\n")

    assert result.exit_code == 0
    assert "1 steps kept" in result.output
    assert "1 steps dropped" in result.output

    data = json.loads((source_dir / "data.json").read_text(encoding="utf-8"))
    assert data["step_count"] == 1
    assert data["steps"][0]["index"] == 1


def test_review_exits_1_for_missing_data_json(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["review", str(tmp_path)])
    assert result.exit_code == 1


# ------------------------------------------------------------------
# sa analyze
# ------------------------------------------------------------------

def test_analyze_calls_analyze_session(tmp_path):
    source_dir = _make_session_dir(tmp_path)
    runner = CliRunner()

    with patch("psr_ai.core.analyzer.analyze_session") as mock_analyze:
        mock_analyze.return_value = 2
        # Patch the import inside cli.py
        with patch("psr_ai.cli.analyze_session", mock_analyze, create=True):
            # We need to patch the import inside the command
            with patch("builtins.__import__") as mock_import:
                # Use a simpler approach: mock the entire analyzer module
                pass

    # Test the actual path: analyzer module importable, function works
    with patch("psr_ai.core.analyzer._PROVIDERS", {
        "anthropic": (MagicMock(return_value="AI desc"), "test-model")
    }):
        result = runner.invoke(
            main,
            ["analyze", str(source_dir), "--no-regen"],
            env={"ANTHROPIC_API_KEY": "test-key"},
        )

    assert result.exit_code == 0
    assert "enriched" in result.output.lower() or "done" in result.output.lower()


def test_analyze_reports_enriched_count(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=3)
    runner = CliRunner()

    with patch("psr_ai.core.analyzer._PROVIDERS", {
        "anthropic": (MagicMock(return_value="A description"), "test-model")
    }):
        result = runner.invoke(
            main,
            ["analyze", str(source_dir), "--no-regen"],
            env={"ANTHROPIC_API_KEY": "test-key"},
        )

    assert result.exit_code == 0
    assert "3" in result.output


def test_analyze_exits_1_for_missing_data_json(tmp_path):
    runner = CliRunner()
    (tmp_path / "screenshots").mkdir()  # Dir exists but no data.json
    result = runner.invoke(main, ["analyze", str(tmp_path)])
    assert result.exit_code == 1


def test_analyze_help_shows_provider_option():
    runner = CliRunner()
    result = runner.invoke(main, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--model" in result.output


# ------------------------------------------------------------------
# sa pub
# ------------------------------------------------------------------

def test_pub_copies_assets(tmp_path):
    source_dir = _make_session_dir(tmp_path)
    # Create required files.
    (source_dir / "report.md").write_text("# Report", encoding="utf-8")
    (source_dir / "action_replay.gif").write_bytes(b"GIF87a")

    dest_root = tmp_path / "docs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["pub", str(source_dir), "--dest", str(dest_root)],
    )

    assert result.exit_code == 0
    assert "Copied to" in result.output


def test_pub_exits_1_for_nonexistent_source(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["pub", str(tmp_path / "does_not_exist")])
    assert result.exit_code != 0


# ------------------------------------------------------------------
# Quiet-by-default output contract (sa start / sa stop)
# ------------------------------------------------------------------

# Real invocations are separate processes; neutralise the inherited env so the
# verbosity of one test never leaks into the next.
_CLEAN_ENV = {"PSR_AI_VERBOSE": "", "PSR_AI_QUIET": ""}


def _invoke(args, **kwargs):
    env = dict(_CLEAN_ENV)
    env.update(kwargs.pop("env", {}))
    # sa start spawns the context listener for real; tests must never do that.
    with patch("psr_ai.cli._start_context_listener", return_value=None):
        return CliRunner().invoke(main, args, env=env, **kwargs)


def _nonempty(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


class TestQuietOutput:

    def test_start_prints_only_the_output_path(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr_20240115_143000.zip"
        with patch("psr_ai.cli.Recorder", return_value=recorder):
            result = _invoke(["start"])
        assert result.exit_code == 0
        assert _nonempty(result.stdout) == [str(tmp_path / "psr_20240115_143000.zip")]

    def test_start_verbose_restores_narration(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        with patch("psr_ai.cli.Recorder", return_value=recorder):
            result = _invoke(["--verbose", "start", "--maxsc", "42"])
        assert result.exit_code == 0
        assert "Recording started." in result.stdout
        assert "Max screenshots: 42" in result.stdout

    def test_start_defaults_to_100_screenshots(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        with patch("psr_ai.cli.Recorder", return_value=recorder):
            _invoke(["start"])
        assert recorder.start_recording.call_args.kwargs["max_screenshots"] == 100

    def test_env_var_can_turn_verbosity_on(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        with patch("psr_ai.cli.Recorder", return_value=recorder):
            result = _invoke(["start"], env={"PSR_AI_VERBOSE": "1"})
        assert "Recording started." in result.stdout

    def test_stop_prints_one_result_line(self, tmp_path):
        zip_path = tmp_path / "sess.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        recorder = MagicMock()
        recorder.stop_recording.return_value = zip_path
        session = MagicMock()
        session.steps = [1, 2, 3]
        report = tmp_path / "sess_output" / "report.md"

        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli.parse_session", return_value=session),
            patch("psr_ai.cli.generate_json_meta", return_value=tmp_path / "data.json"),
            patch("psr_ai.cli.MarkdownGenerator") as markdown,
            patch("psr_ai.cli._build_gif_for_stop"),
            patch("psr_ai.cli._enrich_with_html"),
            patch("psr_ai.cli._suggest_video_analysis"),
        ):
            markdown.return_value.write.return_value = report
            result = _invoke(["stop"])

        assert result.exit_code == 0
        assert _nonempty(result.stdout) == [f"3 steps -> {report}"]

    def test_stop_without_output_keeps_stdout_clean(self):
        recorder = MagicMock()
        recorder.stop_recording.return_value = None
        with patch("psr_ai.cli.Recorder", return_value=recorder):
            result = _invoke(["stop"])
        assert result.exit_code == 1
        assert result.stdout.strip() == ""
        assert "No recording output found" in result.stderr


# ------------------------------------------------------------------
# sa mcp install
# ------------------------------------------------------------------

class TestMcpInstall:

    def test_creates_config_when_absent(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 0
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "psr-ai" in data["mcpServers"]
        assert data["mcpServers"]["psr-ai"]["command"]

    def test_preserves_existing_servers_and_settings(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text(
            json.dumps({
                "mcpServers": {"other": {"command": "node", "args": ["s.js"]}},
                "globalShortcut": "Ctrl+Q",
            }),
            encoding="utf-8",
        )
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 0
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["mcpServers"]["other"] == {"command": "node", "args": ["s.js"]}
        assert data["globalShortcut"] == "Ctrl+Q"
        assert "psr-ai" in data["mcpServers"]

    def test_backs_up_the_previous_config(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text('{"mcpServers": {}}', encoding="utf-8")
        _invoke(["mcp", "install", "--config", str(config)])
        assert (tmp_path / "claude_desktop_config.json.bak").exists()

    def test_second_run_is_idempotent(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        _invoke(["mcp", "install", "--config", str(config)])
        first = config.read_text(encoding="utf-8")
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 0
        assert "Already installed" in result.stdout
        assert config.read_text(encoding="utf-8") == first

    def test_refuses_to_replace_a_conflicting_entry(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text(
            json.dumps({"mcpServers": {"psr-ai": {"command": "old", "args": []}}}),
            encoding="utf-8",
        )
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 1
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["mcpServers"]["psr-ai"]["command"] == "old"

    def test_force_replaces_a_conflicting_entry(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text(
            json.dumps({"mcpServers": {"psr-ai": {"command": "old", "args": []}}}),
            encoding="utf-8",
        )
        result = _invoke(["mcp", "install", "--config", str(config), "--force"])
        assert result.exit_code == 0
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["mcpServers"]["psr-ai"]["command"] != "old"

    def test_dry_run_writes_nothing(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        result = _invoke(["mcp", "install", "--config", str(config), "--dry-run"])
        assert result.exit_code == 0
        assert not config.exists()
        assert "mcpServers" in result.stdout

    def test_rejects_malformed_json_without_clobbering_it(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text("{not json", encoding="utf-8")
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 1
        assert config.read_text(encoding="utf-8") == "{not json"

    def test_refuses_when_mcpservers_is_not_an_object(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        config.write_text('{"mcpServers": ["oops"]}', encoding="utf-8")
        result = _invoke(["mcp", "install", "--config", str(config)])
        assert result.exit_code == 1
        assert config.read_text(encoding="utf-8") == '{"mcpServers": ["oops"]}'

    def test_backup_is_byte_identical_to_the_original(self, tmp_path):
        config = tmp_path / "claude_desktop_config.json"
        original = '\ufeff{"mcpServers": {"other": {"command": "node"}}}'
        config.write_text(original, encoding="utf-8")
        raw = config.read_bytes()
        _invoke(["mcp", "install", "--config", str(config)])
        assert (tmp_path / "claude_desktop_config.json.bak").read_bytes() == raw

    def test_server_block_points_at_the_current_environment(self):
        from psr_ai.cli import _mcp_server_block
        block = _mcp_server_block()
        assert Path(block["command"]).is_absolute()
        assert block["args"][-1] == "mcp"

    def test_config_path_is_os_specific(self):
        from psr_ai.cli import _claude_desktop_config_path
        path = _claude_desktop_config_path()
        assert path.name == "claude_desktop_config.json"
        assert path.parent.name == "Claude"


# ------------------------------------------------------------------
# sa context  (Hidden Context Layer)
# ------------------------------------------------------------------

class TestContextCommands:

    def test_token_prints_a_token(self, tmp_path, monkeypatch):
        from psr_ai.core import context_server
        monkeypatch.setattr(context_server, "_TOKEN_FILE", tmp_path / "token")
        result = _invoke(["context", "token"])
        assert result.exit_code == 0
        assert len(result.stdout.strip()) >= 32

    def test_token_rotate_changes_it(self, tmp_path, monkeypatch):
        from psr_ai.core import context_server
        monkeypatch.setattr(context_server, "_TOKEN_FILE", tmp_path / "token")
        first = _invoke(["context", "token"]).stdout.strip()
        second = _invoke(["context", "token", "--rotate"]).stdout.strip()
        assert first != second

    def test_install_no_launch_prints_manual_steps(self, tmp_path, monkeypatch):
        from psr_ai.core import context_server
        monkeypatch.setattr(context_server, "_TOKEN_FILE", tmp_path / "token")
        result = _invoke(["context", "install", "--no-launch"])
        assert result.exit_code == 0
        assert "chrome://extensions" in result.stdout
        assert "Load unpacked" in result.stdout
        assert "extension" in result.stdout

    def test_install_opens_an_instrumented_browser_by_default(self, tmp_path, monkeypatch):
        from psr_ai.core import context_browser, context_server
        monkeypatch.setattr(context_server, "_TOKEN_FILE", tmp_path / "token")
        target = context_browser.BrowserTarget("Edge", Path("msedge.exe"), tmp_path / "profile")
        with patch("psr_ai.core.context_browser.launch", return_value=(target, 1234)) as launch:
            result = _invoke(["context", "install"])
        assert result.exit_code == 0
        launch.assert_called_once()
        assert "Edge is open" in result.stdout

    def test_install_falls_back_to_manual_when_chrome_only(self, tmp_path, monkeypatch):
        """Chrome ignores --load-extension, so the user gets the manual path."""
        from psr_ai.core import context_browser, context_server
        monkeypatch.setattr(context_server, "_TOKEN_FILE", tmp_path / "token")
        with patch("psr_ai.core.context_browser.launch",
                   side_effect=context_browser.UnsupportedBrowserError("Chrome ignores it")):
            result = _invoke(["context", "install"])
        assert result.exit_code == 0
        assert "Load unpacked" in result.stdout

    def test_browser_command_reports_an_unsupported_browser(self, tmp_path):
        from psr_ai.core import context_browser
        with patch("psr_ai.core.context_browser.launch",
                   side_effect=context_browser.UnsupportedBrowserError("Chrome ignores it")):
            result = _invoke(["context", "browser", "--browser", "chrome"])
        assert result.exit_code == 1
        assert "ignores it" in result.stderr

    def test_install_points_at_a_real_manifest(self):
        from psr_ai.cli import _extension_dir
        assert (_extension_dir() / "manifest.json").is_file()
        assert (_extension_dir() / "background.js").is_file()

    def test_status_reports_idle_without_a_listener(self, tmp_path, monkeypatch):
        from psr_ai.core import context_server
        monkeypatch.setattr(context_server, "_STATE_FILE", tmp_path / "state.json")
        result = _invoke(["context", "status"])
        assert result.exit_code == 0
        assert "not running" in result.stdout

    def test_status_reports_a_live_listener(self, tmp_path, monkeypatch):
        from psr_ai.core import context_server
        monkeypatch.setattr(context_server, "_STATE_FILE", tmp_path / "state.json")
        session_dir = tmp_path / "session_output"
        session_dir.mkdir()
        (session_dir / "context.jsonl").write_text(
            json.dumps({"kind": "console_error", "message": "x"}) + "\n", encoding="utf-8"
        )
        context_server.write_state({
            "pid": 123, "port": 8765, "session_id": "unit",
            "session_dir": str(session_dir), "stop_file": str(session_dir / ".context_stop"),
        })
        result = _invoke(["context", "status"])
        assert "8765" in result.stdout
        assert "1 events" in result.stdout


class TestContextWiring:

    def test_start_launches_the_listener_by_default(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        env = dict(_CLEAN_ENV)
        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli._start_context_listener", return_value=8765) as listener,
        ):
            result = CliRunner().invoke(main, ["start"], env=env)
        assert result.exit_code == 0
        listener.assert_called_once()

    def test_no_context_skips_the_listener(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        env = dict(_CLEAN_ENV)
        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli._start_context_listener") as listener,
        ):
            result = CliRunner().invoke(main, ["start", "--no-context"], env=env)
        assert result.exit_code == 0
        listener.assert_not_called()

    def test_a_failed_listener_never_blocks_recording(self, tmp_path):
        recorder = MagicMock()
        recorder.start_recording.return_value = tmp_path / "psr.zip"
        env = dict(_CLEAN_ENV)
        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli._start_context_listener", return_value=None),
        ):
            result = CliRunner().invoke(main, ["start"], env=env)
        assert result.exit_code == 0
        assert _nonempty(result.stdout) == [str(tmp_path / "psr.zip")]

    def test_stop_signals_the_listener_and_merges(self, tmp_path):
        zip_path = tmp_path / "sess.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        recorder = MagicMock()
        recorder.stop_recording.return_value = zip_path
        session = MagicMock()
        session.steps = [1]

        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli.parse_session", return_value=session),
            patch("psr_ai.cli.generate_json_meta", return_value=tmp_path / "data.json"),
            patch("psr_ai.cli.MarkdownGenerator") as markdown,
            patch("psr_ai.cli._build_gif_for_stop"),
            patch("psr_ai.cli._enrich_with_html"),
            patch("psr_ai.cli._suggest_video_analysis"),
            patch("psr_ai.cli._stop_context_listener") as stop_listener,
            patch("psr_ai.cli._enrich_with_context") as enrich,
        ):
            markdown.return_value.write.return_value = tmp_path / "report.md"
            result = _invoke(["stop"])

        assert result.exit_code == 0
        stop_listener.assert_called_once()
        enrich.assert_called_once()
        assert enrich.call_args[0][2] is False   # clipboard off unless asked

    def test_stop_clipboard_flag_is_forwarded(self, tmp_path):
        zip_path = tmp_path / "sess.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        recorder = MagicMock()
        recorder.stop_recording.return_value = zip_path
        session = MagicMock()
        session.steps = [1]

        with (
            patch("psr_ai.cli.Recorder", return_value=recorder),
            patch("psr_ai.cli.parse_session", return_value=session),
            patch("psr_ai.cli.generate_json_meta", return_value=tmp_path / "data.json"),
            patch("psr_ai.cli.MarkdownGenerator") as markdown,
            patch("psr_ai.cli._build_gif_for_stop"),
            patch("psr_ai.cli._enrich_with_html"),
            patch("psr_ai.cli._suggest_video_analysis"),
            patch("psr_ai.cli._stop_context_listener"),
            patch("psr_ai.cli._enrich_with_context") as enrich,
        ):
            markdown.return_value.write.return_value = tmp_path / "report.md"
            result = _invoke(["stop", "--clipboard"])

        assert result.exit_code == 0
        assert enrich.call_args[0][2] is True

    def test_enrich_writes_both_context_blocks(self, tmp_path):
        from psr_ai.cli import _enrich_with_context
        session_dir = tmp_path / "session_output"
        session_dir.mkdir()
        (session_dir / "context.jsonl").write_text(
            json.dumps({
                "kind": "network_failure", "timestamp": "2026-08-04T10:00:05Z",
                "url": "https://api.example.com/x", "status": 500, "method": "GET",
            }) + "\n",
            encoding="utf-8",
        )
        json_path = session_dir / "data.json"
        json_path.write_text(json.dumps({
            "steps": [{"index": 0, "label": "Step 0", "timestamp": "2026-08-04T10:00:00Z"}],
        }), encoding="utf-8")

        _enrich_with_context(json_path, session_dir, include_clipboard=False)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["browser_context"]["events_total"] == 1
        assert data["steps"][0]["browser_context"]["network_failures"][0]["status"] == 500
        assert "active_window" in data["os_context"]
        assert "clipboard" not in data["os_context"]

    def test_enrich_survives_a_missing_data_json(self, tmp_path):
        from psr_ai.cli import _enrich_with_context
        _enrich_with_context(tmp_path / "absent.json", tmp_path, include_clipboard=False)
