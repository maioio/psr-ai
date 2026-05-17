"""Unit tests for core/watcher.py."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from psr_ai.core.watcher import (
    TriggerEvent,
    WatchConfig,
    WatchResult,
    _check_trigger,
    run_watcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_config(**overrides) -> WatchConfig:
    defaults = dict(
        trigger="an error dialog appeared",
        action="close the error dialog",
        interval=0.01,
        timeout=60.0,
        max_triggers=1,
        provider="anthropic",
        dry_run=True,
    )
    defaults.update(overrides)
    return WatchConfig(**defaults)


def _make_mock_executor(img: bytes = _TINY_PNG) -> MagicMock:
    ex = MagicMock()
    ex.screenshot.return_value = img
    return ex


def _detected_response(detected: bool = True, reasoning: str = "I see it") -> str:
    import json
    return json.dumps({"detected": detected, "reasoning": reasoning})


# ---------------------------------------------------------------------------
# WatchConfig defaults
# ---------------------------------------------------------------------------

class TestWatchConfigDefaults:

    def test_dry_run_true_by_default(self):
        c = WatchConfig(trigger="x", action="y")
        assert c.dry_run is True

    def test_ask_before_action_false_by_default(self):
        c = WatchConfig(trigger="x", action="y")
        assert c.ask_before_action is False

    def test_interval_default(self):
        c = WatchConfig(trigger="x", action="y")
        assert c.interval == 5.0

    def test_max_triggers_default(self):
        c = WatchConfig(trigger="x", action="y")
        assert c.max_triggers == 1

    def test_timeout_default(self):
        c = WatchConfig(trigger="x", action="y")
        assert c.timeout == 3600.0


# ---------------------------------------------------------------------------
# WatchResult.report()
# ---------------------------------------------------------------------------

class TestWatchResultReport:

    def _make_result(self, **kw) -> WatchResult:
        config = _make_config()
        defaults = dict(
            config=config, success=False, stopped_reason="timeout",
        )
        defaults.update(kw)
        return WatchResult(**defaults)

    def test_success_shown(self):
        r = self._make_result(success=True)
        assert "SUCCESS" in r.report()

    def test_timeout_shown(self):
        r = self._make_result(success=False, stopped_reason="timeout")
        assert "TIMEOUT" in r.report() or "timeout" in r.report().lower()

    def test_trigger_count_shown(self):
        r = self._make_result(success=True, triggers_detected=3)
        assert "3" in r.report()

    def test_events_shown(self):
        ev = TriggerEvent(index=1, timestamp=time.time(), reasoning="Saw a red box")
        r = self._make_result(success=True, triggers_detected=1, events=[ev])
        assert "Saw a red box" in r.report()

    def test_error_shown(self):
        r = self._make_result(success=False, error="API timeout")
        assert "API timeout" in r.report()

    def test_trigger_description_shown(self):
        r = self._make_result()
        assert "error dialog" in r.report()


# ---------------------------------------------------------------------------
# _check_trigger
# ---------------------------------------------------------------------------

class TestCheckTrigger:

    def test_returns_true_when_detected(self):
        config = _make_config()
        with patch("psr_ai.core.watcher._call_anthropic",
                   return_value=_detected_response(True, "I see an error dialog")):
            detected, reasoning = _check_trigger(config, "claude-sonnet-4-6", _TINY_PNG)
        assert detected is True
        assert "error dialog" in reasoning

    def test_returns_false_when_not_detected(self):
        config = _make_config()
        with patch("psr_ai.core.watcher._call_anthropic",
                   return_value=_detected_response(False, "Screen looks normal")):
            detected, reasoning = _check_trigger(config, "claude-sonnet-4-6", _TINY_PNG)
        assert detected is False

    def test_malformed_json_falls_back_gracefully(self):
        config = _make_config()
        with patch("psr_ai.core.watcher._call_anthropic", return_value="yes, I see it"):
            detected, _ = _check_trigger(config, "claude-sonnet-4-6", _TINY_PNG)
        assert detected is True

    def test_no_in_response_means_false(self):
        config = _make_config()
        with patch("psr_ai.core.watcher._call_anthropic", return_value="No, nothing detected"):
            detected, _ = _check_trigger(config, "claude-sonnet-4-6", _TINY_PNG)
        assert detected is False

    def test_uses_openai_when_provider_openai(self):
        config = _make_config(provider="openai")
        with patch("psr_ai.core.watcher._call_openai",
                   return_value=_detected_response(True)) as mock_oa:
            _check_trigger(config, "gpt-4o", _TINY_PNG)
        mock_oa.assert_called_once()

    def test_unknown_provider_raises(self):
        config = _make_config(provider="cohere")
        with pytest.raises(ValueError, match="Unknown provider"):
            _check_trigger(config, "model", _TINY_PNG)


# ---------------------------------------------------------------------------
# run_watcher
# ---------------------------------------------------------------------------

class TestRunWatcher:

    def _patch_watcher(self, detected_sequence, action_result=(True, "task_complete")):
        """Return a context manager patching executor and LLM."""
        mock_executor = _make_mock_executor()
        detect_iter = iter(detected_sequence)

        def fake_check(cfg, model, img):
            try:
                d = next(detect_iter)
            except StopIteration:
                d = False
            return d, "test reasoning"

        def fake_run_action(cfg, echo, prompt_fn):
            return action_result

        return (
            patch("psr_ai.core.watcher._get_executor", return_value=mock_executor),
            patch("psr_ai.core.watcher._check_trigger", side_effect=fake_check),
            patch("psr_ai.core.watcher._run_action_agent", side_effect=fake_run_action),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        )

    def test_returns_watch_result(self):
        config = _make_config(max_triggers=1, timeout=1.0)
        patches = self._patch_watcher([True])
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert isinstance(result, WatchResult)

    def test_detects_trigger_and_sets_success(self):
        config = _make_config(max_triggers=1)
        patches = self._patch_watcher([True])
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.triggers_detected == 1
        assert result.success is True

    def test_stops_after_max_triggers(self):
        config = _make_config(max_triggers=2)
        patches = self._patch_watcher([True, True, True])
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.triggers_detected == 2
        assert result.stopped_reason == "max_triggers"

    def test_timeout_stops_loop(self):
        config = _make_config(interval=0.01, timeout=0.05, max_triggers=999)
        patches = self._patch_watcher([False] * 1000)
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.stopped_reason == "timeout"
        assert result.success is False

    def test_dry_run_does_not_call_action_agent(self):
        config = _make_config(dry_run=True, max_triggers=1)
        mock_executor = _make_mock_executor()

        def fake_check(cfg, model, img):
            return True, "detected"

        with (
            patch("psr_ai.core.watcher._get_executor", return_value=mock_executor),
            patch("psr_ai.core.watcher._check_trigger", side_effect=fake_check),
            patch("psr_ai.core.watcher._run_action_agent") as mock_action,
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_watcher(config, echo_fn=lambda m: None)

        mock_action.assert_not_called()

    def test_no_dry_run_calls_action_agent(self):
        config = _make_config(dry_run=False, max_triggers=1)
        mock_executor = _make_mock_executor()
        action_calls = []

        def fake_check(cfg, model, img):
            return True, "detected"

        def fake_action(cfg, echo, prompt_fn):
            action_calls.append(1)
            return True, "task_complete"

        with (
            patch("psr_ai.core.watcher._get_executor", return_value=mock_executor),
            patch("psr_ai.core.watcher._check_trigger", side_effect=fake_check),
            patch("psr_ai.core.watcher._run_action_agent", side_effect=fake_action),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            run_watcher(config, echo_fn=lambda m: None)

        assert len(action_calls) == 1

    def test_keyboard_interrupt_handled(self):
        config = _make_config()
        with (
            patch("psr_ai.core.watcher._get_executor", side_effect=KeyboardInterrupt),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.stopped_reason == "user_interrupt"
        assert result.success is False

    def test_never_raises(self):
        config = _make_config()
        with patch("psr_ai.core.watcher._get_executor", side_effect=RuntimeError("no display")):
            result = run_watcher(config, echo_fn=lambda m: None)
        assert isinstance(result, WatchResult)

    def test_missing_api_key_returns_error(self):
        import os as _os
        config = _make_config(provider="anthropic")
        env = {k: v for k, v in _os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.success is False
        assert "ANTHROPIC_API_KEY" in result.error

    def test_events_populated_on_detection(self):
        config = _make_config(max_triggers=1)
        patches = self._patch_watcher([True])
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert len(result.events) == 1
        assert result.events[0].index == 1

    def test_checks_performed_counted(self):
        config = _make_config(max_triggers=1)
        patches = self._patch_watcher([False, False, True])
        with patches[0], patches[1], patches[2], patches[3]:
            result = run_watcher(config, echo_fn=lambda m: None)
        assert result.checks_performed == 3

    def test_detection_error_skipped_gracefully(self):
        config = _make_config(max_triggers=1, timeout=0.2)
        mock_executor = _make_mock_executor()
        call_count = [0]

        def fake_check(cfg, model, img):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("LLM timeout")
            return True, "found it"

        with (
            patch("psr_ai.core.watcher._get_executor", return_value=mock_executor),
            patch("psr_ai.core.watcher._check_trigger", side_effect=fake_check),
            patch("psr_ai.core.watcher._run_action_agent", return_value=(True, "task_complete")),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            result = run_watcher(config, echo_fn=lambda m: None)

        assert result.triggers_detected == 1
