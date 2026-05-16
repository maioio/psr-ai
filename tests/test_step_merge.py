"""Unit tests for merge_consecutive_steps and related helpers in generator.py."""

from __future__ import annotations

import pytest

from psr_ai.core.generator import (
    _collapse_run,
    _repetitive_prefix,
    _should_merge,
    _within_threshold,
    merge_consecutive_steps,
)


# ---------------------------------------------------------------------------
# _repetitive_prefix
# ---------------------------------------------------------------------------

class TestRepetitivePrefix:

    def test_scroll_detected(self):
        assert _repetitive_prefix("scroll down") == "scroll"

    def test_type_detected(self):
        assert _repetitive_prefix("type hello") == "type"

    def test_key_detected(self):
        assert _repetitive_prefix("key backspace") == "key"

    def test_press_detected(self):
        assert _repetitive_prefix("press enter") == "press"

    def test_input_detected(self):
        assert _repetitive_prefix("input text") == "input"

    def test_mouse_move_detected(self):
        assert _repetitive_prefix("mouse move to button") == "mouse move"

    def test_click_not_repetitive(self):
        assert _repetitive_prefix("left click on submit") is None

    def test_empty_string_not_repetitive(self):
        assert _repetitive_prefix("") is None

    def test_uppercase_not_matched(self):
        # _repetitive_prefix receives pre-lowercased text.
        assert _repetitive_prefix("Scroll down") is None


# ---------------------------------------------------------------------------
# _within_threshold
# ---------------------------------------------------------------------------

class TestWithinThreshold:

    def test_same_second_within(self):
        ts = "1/15/2024 2:30:00 PM"
        assert _within_threshold(ts, ts) is True

    def test_two_seconds_apart_within(self):
        assert _within_threshold(
            "1/15/2024 2:30:00 PM",
            "1/15/2024 2:30:02 PM",
        ) is True

    def test_six_seconds_apart_outside(self):
        assert _within_threshold(
            "1/15/2024 2:30:00 PM",
            "1/15/2024 2:30:06 PM",
        ) is False

    def test_missing_timestamp_a_is_permissive(self):
        assert _within_threshold(None, "1/15/2024 2:30:00 PM") is True

    def test_missing_timestamp_b_is_permissive(self):
        assert _within_threshold("1/15/2024 2:30:00 PM", None) is True

    def test_both_missing_is_permissive(self):
        assert _within_threshold(None, None) is True

    def test_unparseable_timestamp_is_permissive(self):
        assert _within_threshold("not a date", "also not a date") is True

    def test_exactly_at_threshold_is_within(self):
        # _MERGE_THRESHOLD_SECONDS is 5.
        assert _within_threshold(
            "1/15/2024 2:30:00 PM",
            "1/15/2024 2:30:05 PM",
        ) is True

    def test_one_second_over_threshold_is_outside(self):
        assert _within_threshold(
            "1/15/2024 2:30:00 PM",
            "1/15/2024 2:30:06 PM",
        ) is False


# ---------------------------------------------------------------------------
# _should_merge
# ---------------------------------------------------------------------------

class TestShouldMerge:

    def _step(self, action: str, prog: str = "Chrome", ts: str = "1/15/2024 2:30:00 PM") -> dict:
        return {"action_text": action, "program": prog, "timestamp": ts}

    def test_same_prefix_same_prog_within_threshold(self):
        a = self._step("scroll down", "Chrome", "1/15/2024 2:30:00 PM")
        b = self._step("scroll up", "Chrome", "1/15/2024 2:30:02 PM")
        assert _should_merge(a, b) is True

    def test_different_program_not_merged(self):
        a = self._step("scroll down", "Chrome")
        b = self._step("scroll down", "Notepad")
        assert _should_merge(a, b) is False

    def test_different_prefix_not_merged(self):
        a = self._step("scroll down")
        b = self._step("type hello")
        assert _should_merge(a, b) is False

    def test_non_repetitive_not_merged(self):
        a = self._step("left click on submit")
        b = self._step("left click on cancel")
        assert _should_merge(a, b) is False

    def test_outside_threshold_not_merged(self):
        a = self._step("scroll down", "Chrome", "1/15/2024 2:30:00 PM")
        b = self._step("scroll down", "Chrome", "1/15/2024 2:30:10 PM")
        assert _should_merge(a, b) is False

    def test_missing_timestamps_merged(self):
        a = {"action_text": "scroll down", "program": "Chrome", "timestamp": None}
        b = {"action_text": "scroll down", "program": "Chrome", "timestamp": None}
        assert _should_merge(a, b) is True


# ---------------------------------------------------------------------------
# _collapse_run
# ---------------------------------------------------------------------------

class TestCollapseRun:

    def _step(self, index: int, action: str, prog: str = "Chrome") -> dict:
        return {
            "index": index,
            "label": action,
            "timestamp": "1/15/2024 2:30:00 PM",
            "ui_element": "",
            "program": prog,
            "action_text": action,
            "screenshot": f"screenshots/step_{index:03d}.png",
        }

    def test_single_step_passthrough(self):
        s = self._step(0, "left click")
        result = _collapse_run([s])
        assert result is s

    def test_merged_entry_has_range_key(self):
        steps = [self._step(0, "scroll down"), self._step(1, "scroll down")]
        result = _collapse_run(steps)
        assert "_merged_range" in result
        assert result["_merged_range"] == "0-1"

    def test_scroll_summary_text(self):
        steps = [self._step(i, "scroll down") for i in range(3)]
        result = _collapse_run(steps)
        assert "scrolling" in result["action_text"].lower()
        assert "3" in result["action_text"]

    def test_typing_summary_text(self):
        steps = [self._step(i, "type x") for i in range(4)]
        result = _collapse_run(steps)
        assert "text input" in result["action_text"].lower()
        assert "4" in result["action_text"]

    def test_generic_summary_text(self):
        steps = [self._step(i, "input data") for i in range(2)]
        result = _collapse_run(steps)
        assert "2" in result["action_text"]

    def test_has_screenshot_when_any_step_has_one(self):
        steps = [
            {**self._step(0, "scroll down"), "screenshot": ""},
            {**self._step(1, "scroll down"), "screenshot": "screenshots/step_001.png"},
        ]
        result = _collapse_run(steps)
        assert result["screenshot"] != ""

    def test_no_screenshot_when_none_have_one(self):
        steps = [
            {**self._step(i, "scroll down"), "screenshot": ""} for i in range(2)
        ]
        result = _collapse_run(steps)
        assert result["screenshot"] == ""


# ---------------------------------------------------------------------------
# merge_consecutive_steps (end-to-end)
# ---------------------------------------------------------------------------

class TestMergeConsecutiveSteps:

    def _step(self, index: int, action: str, prog: str = "Chrome",
              ts: str = "1/15/2024 2:30:00 PM") -> dict:
        return {
            "index": index,
            "label": action,
            "timestamp": ts,
            "ui_element": "",
            "program": prog,
            "action_text": action,
            "screenshot": f"screenshots/step_{index:03d}.png",
        }

    def test_empty_list_returns_empty(self):
        assert merge_consecutive_steps([]) == []

    def test_single_step_unchanged(self):
        s = self._step(0, "left click")
        result = merge_consecutive_steps([s])
        assert len(result) == 1
        assert result[0] is s

    def test_non_repetitive_steps_all_kept(self):
        steps = [self._step(i, "left click") for i in range(3)]
        result = merge_consecutive_steps(steps)
        assert len(result) == 3

    def test_consecutive_scrolls_merged(self):
        steps = [
            self._step(0, "scroll down", "Chrome", "1/15/2024 2:30:00 PM"),
            self._step(1, "scroll down", "Chrome", "1/15/2024 2:30:01 PM"),
            self._step(2, "scroll down", "Chrome", "1/15/2024 2:30:02 PM"),
        ]
        result = merge_consecutive_steps(steps)
        assert len(result) == 1
        assert result[0]["_merged_range"] == "0-2"

    def test_interleaved_program_breaks_merge(self):
        steps = [
            self._step(0, "scroll down", "Chrome", "1/15/2024 2:30:00 PM"),
            self._step(1, "scroll down", "Notepad", "1/15/2024 2:30:01 PM"),
            self._step(2, "scroll down", "Chrome", "1/15/2024 2:30:02 PM"),
        ]
        result = merge_consecutive_steps(steps)
        assert len(result) == 3

    def test_mixed_repetitive_and_normal(self):
        steps = [
            self._step(0, "left click"),
            self._step(1, "scroll down", ts="1/15/2024 2:30:00 PM"),
            self._step(2, "scroll down", ts="1/15/2024 2:30:01 PM"),
            self._step(3, "left click"),
        ]
        result = merge_consecutive_steps(steps)
        # click, merged-scrolls, click -> 3 entries.
        assert len(result) == 3

    def test_time_gap_breaks_scroll_run(self):
        steps = [
            self._step(0, "scroll down", ts="1/15/2024 2:30:00 PM"),
            self._step(1, "scroll down", ts="1/15/2024 2:30:01 PM"),
            self._step(2, "scroll down", ts="1/15/2024 2:31:00 PM"),  # 59s gap
        ]
        result = merge_consecutive_steps(steps)
        # First two merge; third stays separate.
        assert len(result) == 2

    def test_output_preserves_program(self):
        steps = [
            self._step(0, "type a", "VS Code", "1/15/2024 2:30:00 PM"),
            self._step(1, "type b", "VS Code", "1/15/2024 2:30:01 PM"),
        ]
        result = merge_consecutive_steps(steps)
        assert result[0]["program"] == "VS Code"

    def test_non_repetitive_preserved_with_original_index(self):
        steps = [self._step(5, "left click")]
        result = merge_consecutive_steps(steps)
        assert result[0]["index"] == 5
