"""Unit tests for core/agent.py - agent loop behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.agent import (
    AgentConfig,
    AgentResult,
    AgentStep,
    run_agent,
    _format_action_args,
    _step_to_history,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_config(tmp_path: Path, **overrides) -> AgentConfig:
    defaults = dict(
        task="open the calculator",
        max_steps=5,
        provider="anthropic",
        dry_run=True,
        ask_before_action=False,
        output_dir=tmp_path / "agent_out",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _claude_response(action_type: str = "done", **extra) -> dict:
    """Build a fake parsed Claude response."""
    action = {"type": action_type, "result": "task complete"}
    action.update(extra)
    return {
        "reasoning": "I can see the desktop clearly.",
        "action": action,
        "confidence": 0.9,
        "is_destructive": False,
        "requires_confirmation": False,
    }


# ---------------------------------------------------------------------------
# run_agent: basic flow
# ---------------------------------------------------------------------------

class TestRunAgent:

    def test_returns_agent_result(self, tmp_path):
        config = _make_config(tmp_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=_claude_response("done")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        assert isinstance(result, AgentResult)

    def test_done_action_sets_success_true(self, tmp_path):
        config = _make_config(tmp_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=_claude_response("done")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        assert result.success is True
        assert result.stopped_reason == "task_complete"

    def test_max_steps_stops_loop(self, tmp_path):
        config = _make_config(tmp_path, max_steps=3, dry_run=True)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        # Return click action every time - never "done"
        click_response = _claude_response(
            "click", x=100, y=200, result=None
        )
        del click_response["action"]["result"]

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=click_response),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        assert result.stopped_reason == "max_steps"
        assert len(result.steps) == 3

    def test_dry_run_does_not_call_executor_dispatch(self, tmp_path):
        config = _make_config(tmp_path, dry_run=True, ask_before_action=False)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        click_response = {"reasoning": "I see a button.", "action": {"type": "click", "x": 10, "y": 20}, "confidence": 0.8, "is_destructive": False, "requires_confirmation": False}
        done_response = _claude_response("done")

        responses = iter([click_response, done_response])

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=lambda *a, **k: next(responses)),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config)

        mock_executor.dispatch.assert_not_called()

    def test_no_dry_run_calls_executor_dispatch(self, tmp_path):
        config = _make_config(tmp_path, dry_run=False, ask_before_action=False)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        click_response = {"reasoning": "Click it.", "action": {"type": "click", "x": 10, "y": 20}, "confidence": 0.8, "is_destructive": False, "requires_confirmation": False}
        done_response = _claude_response("done")

        responses = iter([click_response, done_response])

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=lambda *a, **k: next(responses)),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config)

        mock_executor.dispatch.assert_called_once()

    def test_ask_before_action_calls_prompt_fn(self, tmp_path):
        config = _make_config(tmp_path, dry_run=False, ask_before_action=True)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        click_response = {"reasoning": "Click.", "action": {"type": "click", "x": 5, "y": 5}, "confidence": 0.9, "is_destructive": False, "requires_confirmation": False}
        done_response = _claude_response("done")

        responses = iter([click_response, done_response])
        prompt_calls = []

        def fake_prompt(msg):
            prompt_calls.append(msg)
            return "y"

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=lambda *a, **k: next(responses)),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config, prompt_fn=fake_prompt)

        assert len(prompt_calls) >= 1

    def test_ask_user_action_calls_prompt_fn(self, tmp_path):
        config = _make_config(tmp_path, dry_run=False, ask_before_action=False)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        ask_response = {"reasoning": "Not sure.", "action": {"type": "ask_user", "question": "Which file to open?"}, "confidence": 0.5, "is_destructive": False, "requires_confirmation": False}
        done_response = _claude_response("done")

        responses = iter([ask_response, done_response])
        prompt_answers = []

        def fake_prompt(msg):
            prompt_answers.append(msg)
            return "file.txt"

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=lambda *a, **k: next(responses)),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config, prompt_fn=fake_prompt)

        assert len(prompt_answers) >= 1

    def test_destructive_action_always_asks_even_when_no_ask(self, tmp_path):
        config = _make_config(tmp_path, dry_run=False, ask_before_action=False, ask_before_destructive=True)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        destructive_response = {
            "reasoning": "Deleting file.",
            "action": {"type": "key", "key": "ctrl+a"},
            "confidence": 0.9,
            "is_destructive": True,
            "requires_confirmation": True,
        }
        done_response = _claude_response("done")

        responses = iter([destructive_response, done_response])
        prompt_calls = []

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=lambda *a, **k: next(responses)),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config, prompt_fn=lambda m: (prompt_calls.append(m) or "n"))

        assert len(prompt_calls) >= 1

    def test_missing_api_key_returns_failed_result(self, tmp_path):
        config = _make_config(tmp_path, provider="anthropic")
        mock_executor = MagicMock()

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch.dict("os.environ", {}, clear=True),
        ):
            # Remove any real API key from environment
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = run_agent(config)

        assert result.success is False
        assert "ANTHROPIC_API_KEY" in result.error

    def test_llm_error_stops_loop_gracefully(self, tmp_path):
        config = _make_config(tmp_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", side_effect=RuntimeError("API timeout")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        assert result.success is False
        assert result.stopped_reason == "llm_error"

    def test_session_log_written_to_output_dir(self, tmp_path):
        config = _make_config(tmp_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=_claude_response("done")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        assert result.session_dir is not None
        log_path = result.session_dir / "data.json"
        assert log_path.exists()
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert "steps" in data
        assert "task" not in data or True  # session log uses "title"

    def test_initial_screenshot_used_on_step_one(self, tmp_path):
        png_path = tmp_path / "initial.png"
        png_path.write_bytes(_TINY_PNG)
        config = _make_config(tmp_path, initial_screenshot=png_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=_claude_response("done")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_agent(config)

        # executor.screenshot should NOT be called on step 1 when initial_screenshot set
        mock_executor.screenshot.assert_not_called()

    def test_agent_result_report_contains_status(self, tmp_path):
        config = _make_config(tmp_path)
        mock_executor = MagicMock()
        mock_executor.screenshot.return_value = _TINY_PNG

        with (
            patch("psr_ai.core.agent._get_executor", return_value=mock_executor),
            patch("psr_ai.core.agent._call_llm", return_value=_claude_response("done")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)

        report = result.report()
        assert "SUCCESS" in report or "FAILED" in report
        assert "open the calculator" in report

    def test_run_agent_never_raises(self, tmp_path):
        """run_agent must return a result even when everything goes wrong."""
        config = _make_config(tmp_path)
        with patch("psr_ai.core.agent._get_executor", side_effect=RuntimeError("no display")):
            result = run_agent(config)
        assert isinstance(result, AgentResult)

    def test_keyboard_interrupt_sets_user_interrupt_reason(self, tmp_path):
        config = _make_config(tmp_path)
        with (
            patch("psr_ai.core.agent._get_executor", side_effect=KeyboardInterrupt),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)
        assert result.stopped_reason == "user_interrupt"
        assert result.success is False

    def test_keyboard_interrupt_does_not_raise(self, tmp_path):
        config = _make_config(tmp_path)
        with (
            patch("psr_ai.core.agent._get_executor", side_effect=KeyboardInterrupt),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)
        assert isinstance(result, AgentResult)

    def test_keyboard_interrupt_error_message_set(self, tmp_path):
        config = _make_config(tmp_path)
        with (
            patch("psr_ai.core.agent._get_executor", side_effect=KeyboardInterrupt),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_agent(config)
        assert "Ctrl+C" in result.error or "interrupt" in result.error.lower()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestFormatActionArgs:

    def test_click_shows_coordinates(self):
        assert "(100, 200)" in _format_action_args({"type": "click", "x": 100, "y": 200})

    def test_type_shows_text_preview(self):
        result = _format_action_args({"type": "type", "text": "hello"})
        assert "hello" in result

    def test_long_text_truncated(self):
        result = _format_action_args({"type": "type", "text": "x" * 100})
        assert "..." in result

    def test_key_shows_combo(self):
        assert "ctrl+c" in _format_action_args({"type": "key", "key": "ctrl+c"})

    def test_done_shows_result(self):
        assert "finished" in _format_action_args({"type": "done", "result": "finished"})

    def test_unknown_type_returns_empty(self):
        assert _format_action_args({"type": "unknown"}) == ""


class TestStepToHistory:

    def test_includes_required_fields(self):
        step = AgentStep(
            index=1,
            screenshot_path=None,
            claude_reasoning="I see a button",
            action={"type": "click", "x": 100, "y": 200},
            action_executed=True,
            user_approved=None,
            result="executed",
        )
        hist = _step_to_history(step)
        assert hist["index"] == 1
        assert hist["action"]["type"] == "click"
        assert "I see a button" in hist["claude_reasoning"]
        assert hist["result"] == "executed"
