"""CLI integration tests for the `sa agent` command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from psr_ai.cli import main
from psr_ai.core.agent import AgentResult, AgentStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_success_result(task: str = "test task", tmp_path: Path | None = None) -> AgentResult:
    result = AgentResult(
        task=task,
        success=True,
        stopped_reason="task_complete",
        session_dir=tmp_path,
    )
    result.steps = [
        AgentStep(
            index=1,
            screenshot_path=None,
            claude_reasoning="I completed the task.",
            action={"type": "done", "result": "done"},
            action_executed=False,
            user_approved=None,
            result="task complete",
        )
    ]
    return result


def _make_failed_result(task: str = "test task") -> AgentResult:
    return AgentResult(
        task=task,
        success=False,
        stopped_reason="max_steps",
        error="",
    )


# ---------------------------------------------------------------------------
# Help and basic option parsing
# ---------------------------------------------------------------------------

class TestAgentHelp:

    def test_help_shows_task_argument(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert result.exit_code == 0
        assert "TASK" in result.output

    def test_help_shows_dry_run_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert "--dry-run" in result.output or "dry-run" in result.output

    def test_help_shows_no_dry_run_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert "--no-dry-run" in result.output

    def test_help_shows_max_steps_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert "--max-steps" in result.output

    def test_help_shows_provider_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert "--provider" in result.output

    def test_help_shows_screenshot_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--help"])
        assert "--screenshot" in result.output


# ---------------------------------------------------------------------------
# Dry-run mode (default)
# ---------------------------------------------------------------------------

class TestAgentDryRun:

    def test_dry_run_exits_0_on_success(self, tmp_path):
        runner = CliRunner()
        with patch("psr_ai.core.agent.run_agent", return_value=_make_success_result(tmp_path=tmp_path)):
            result = runner.invoke(
                main,
                ["agent", "open calculator", "--dry-run"],
            )
        assert result.exit_code == 0

    def test_dry_run_exits_1_on_failure(self, tmp_path):
        runner = CliRunner()

        with (
            patch("psr_ai.core.agent.run_agent", return_value=_make_failed_result()),
        ):
            result = runner.invoke(
                main,
                ["agent", "failing task", "--dry-run"],
            )

        assert result.exit_code == 1

    def test_output_contains_task(self, tmp_path):
        runner = CliRunner()

        with patch("psr_ai.core.agent.run_agent", return_value=_make_success_result("my unique task")):
            result = runner.invoke(main, ["agent", "my unique task"])

        assert "my unique task" in result.output

    def test_warning_printed_in_no_dry_run_no_ask_mode(self, tmp_path):
        runner = CliRunner()

        with patch("psr_ai.core.agent.run_agent", return_value=_make_success_result()):
            result = runner.invoke(
                main,
                ["agent", "task", "--no-dry-run", "--no-ask"],
            )

        assert "autonomous" in result.output.lower() or "WARNING" in result.output


# ---------------------------------------------------------------------------
# Screenshot option
# ---------------------------------------------------------------------------

class TestAgentScreenshot:

    def test_screenshot_file_is_passed_to_config(self, tmp_path):
        png_path = tmp_path / "screen.png"
        png_path.write_bytes(b"\x89PNG")
        runner = CliRunner()
        received_configs = []

        def fake_run_agent(config, **kwargs):
            received_configs.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run_agent):
            runner.invoke(
                main,
                ["agent", "fix this", "--screenshot", str(png_path)],
            )

        assert received_configs
        assert received_configs[0].initial_screenshot == png_path

    def test_nonexistent_screenshot_fails(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "task", "--screenshot", str(tmp_path / "missing.png")],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Config propagation
# ---------------------------------------------------------------------------

class TestAgentConfigPropagation:

    def test_max_steps_propagated(self, tmp_path):
        runner = CliRunner()
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run):
            runner.invoke(main, ["agent", "task", "--max-steps", "7"])

        assert received[0].max_steps == 7

    def test_provider_propagated(self, tmp_path):
        runner = CliRunner()
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run):
            runner.invoke(main, ["agent", "task", "--provider", "openai"])

        assert received[0].provider == "openai"

    def test_dry_run_true_by_default(self, tmp_path):
        runner = CliRunner()
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run):
            runner.invoke(main, ["agent", "task"])

        assert received[0].dry_run is True

    def test_no_dry_run_sets_false(self, tmp_path):
        runner = CliRunner()
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run):
            runner.invoke(main, ["agent", "task", "--no-dry-run"])

        assert received[0].dry_run is False

    def test_output_dir_propagated(self, tmp_path):
        runner = CliRunner()
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_success_result()

        with patch("psr_ai.core.agent.run_agent", side_effect=fake_run):
            runner.invoke(main, ["agent", "task", "--output-dir", str(tmp_path)])

        assert received[0].output_dir == tmp_path
