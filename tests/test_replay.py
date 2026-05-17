"""Unit tests for core/replay.py."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.replay import (
    ReplayConfig,
    ReplayResult,
    _build_task_from_session,
    _load_session_meta,
    run_replay,
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


def _make_session_zip(
    tmp_path: Path,
    title: str = "Test Session",
    steps: list[dict] | None = None,
    include_screenshot: bool = True,
) -> Path:
    """Build a minimal ZIP with data.json and optional screenshot."""
    if steps is None:
        steps = [
            {"index": 0, "label": "Step 0", "action_text": "Open calculator", "program": "Desktop", "timestamp": ""},
            {"index": 1, "label": "Step 1", "action_text": "Click button 5", "program": "Calculator", "timestamp": ""},
        ]
    data = {
        "title": title,
        "source": "test.zip",
        "generated_at": "2026-01-01T00:00:00Z",
        "step_count": len(steps),
        "steps": steps,
    }
    zip_path = tmp_path / "session.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("data.json", json.dumps(data))
        if include_screenshot:
            zf.writestr("screenshots/step_000.png", _TINY_PNG)
    return zip_path


def _make_agent_result(success: bool = True) -> MagicMock:
    ar = MagicMock()
    ar.success = success
    ar.stopped_reason = "task_complete" if success else "max_steps"
    ar.steps = []
    ar.session_dir = None
    ar.error = ""
    return ar


# ---------------------------------------------------------------------------
# _load_session_meta
# ---------------------------------------------------------------------------

class TestLoadSessionMeta:

    def test_returns_title_steps_screenshot(self, tmp_path):
        zip_path = _make_session_zip(tmp_path, title="My Workflow")
        title, steps, img = _load_session_meta(zip_path)
        assert title == "My Workflow"
        assert len(steps) == 2
        assert img == _TINY_PNG

    def test_no_screenshot_returns_none(self, tmp_path):
        zip_path = _make_session_zip(tmp_path, include_screenshot=False)
        _, _, img = _load_session_meta(zip_path)
        assert img is None

    def test_raises_on_invalid_zip(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            _load_session_meta(bad)

    def test_raises_when_no_data_json(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("screenshots/step_000.png", _TINY_PNG)
        with pytest.raises(ValueError, match="No data.json"):
            _load_session_meta(zip_path)

    def test_steps_list_returned_correctly(self, tmp_path):
        steps = [{"index": i, "label": f"Step {i}", "action_text": f"Action {i}", "program": ""} for i in range(5)]
        zip_path = _make_session_zip(tmp_path, steps=steps)
        _, loaded_steps, _ = _load_session_meta(zip_path)
        assert len(loaded_steps) == 5
        assert loaded_steps[2]["action_text"] == "Action 2"

    def test_title_defaults_to_stem_when_missing(self, tmp_path):
        zip_path = tmp_path / "mysession.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.json", json.dumps({"steps": []}))
        title, _, _ = _load_session_meta(zip_path)
        assert title == "mysession"


# ---------------------------------------------------------------------------
# _build_task_from_session
# ---------------------------------------------------------------------------

class TestBuildTaskFromSession:

    def test_includes_title(self):
        task = _build_task_from_session("My Workflow", [])
        assert "My Workflow" in task

    def test_includes_step_actions(self):
        steps = [{"action_text": "Click Save button", "program": "Word", "label": "Step 1"}]
        task = _build_task_from_session("Test", steps)
        assert "Click Save button" in task

    def test_includes_program_name(self):
        steps = [{"action_text": "Click OK", "program": "Notepad", "label": "Step 1"}]
        task = _build_task_from_session("Test", steps)
        assert "Notepad" in task

    def test_caps_at_30_steps(self):
        steps = [{"action_text": f"Action {i}", "program": "", "label": f"Step {i}"} for i in range(50)]
        task = _build_task_from_session("Test", steps)
        assert "20 more steps" in task

    def test_no_truncation_when_under_30(self):
        steps = [{"action_text": f"Action {i}", "program": "", "label": f"Step {i}"} for i in range(10)]
        task = _build_task_from_session("Test", steps)
        assert "more steps" not in task

    def test_instructs_visual_adaptation(self):
        task = _build_task_from_session("Test", [])
        assert "Adapt" in task or "adapt" in task


# ---------------------------------------------------------------------------
# ReplayResult.report()
# ---------------------------------------------------------------------------

class TestReplayResultReport:

    def test_success_shown(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        r = ReplayResult(zip_path=zip_path, success=True, session_title="T", total_session_steps=2)
        assert "SUCCESS" in r.report()

    def test_failure_shown(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        r = ReplayResult(zip_path=zip_path, success=False, session_title="T", total_session_steps=2)
        assert "FAILED" in r.report()

    def test_error_shown(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        r = ReplayResult(zip_path=zip_path, success=False, error="API timeout")
        assert "API timeout" in r.report()

    def test_agent_result_steps_shown(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        ar = _make_agent_result(success=True)
        ar.steps = [MagicMock(), MagicMock()]
        r = ReplayResult(zip_path=zip_path, success=True, agent_result=ar)
        assert "2" in r.report()


# ---------------------------------------------------------------------------
# run_replay
# ---------------------------------------------------------------------------

class TestRunReplay:

    def test_returns_replay_result(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", return_value=_make_agent_result(True)):
            result = run_replay(config, echo_fn=lambda m: None)
        assert isinstance(result, ReplayResult)

    def test_success_propagated(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", return_value=_make_agent_result(True)):
            result = run_replay(config, echo_fn=lambda m: None)
        assert result.success is True

    def test_failure_propagated(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", return_value=_make_agent_result(False)):
            result = run_replay(config, echo_fn=lambda m: None)
        assert result.success is False

    def test_session_title_populated(self, tmp_path):
        zip_path = _make_session_zip(tmp_path, title="Unique Title XYZ")
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", return_value=_make_agent_result(True)):
            result = run_replay(config, echo_fn=lambda m: None)
        assert result.session_title == "Unique Title XYZ"

    def test_total_steps_populated(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", return_value=_make_agent_result(True)):
            result = run_replay(config, echo_fn=lambda m: None)
        assert result.total_session_steps == 2

    def test_never_raises_on_bad_zip(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        config = ReplayConfig(zip_path=bad, dry_run=True)
        result = run_replay(config, echo_fn=lambda m: None)
        assert isinstance(result, ReplayResult)
        assert result.success is False
        assert result.error != ""

    def test_keyboard_interrupt_handled(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True)
        with patch("psr_ai.core.replay._run_agent", side_effect=KeyboardInterrupt):
            result = run_replay(config, echo_fn=lambda m: None)
        assert result.success is False
        assert "Ctrl+C" in result.error or "interrupt" in result.error.lower()

    def test_agent_config_uses_replay_max_steps(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True, max_steps=99)
        captured = []

        def fake_run_agent(agent_config, **kwargs):
            captured.append(agent_config)
            return _make_agent_result(True)

        with patch("psr_ai.core.replay._run_agent", side_effect=fake_run_agent):
            run_replay(config, echo_fn=lambda m: None)

        assert captured[0].max_steps == 99

    def test_agent_config_dry_run_propagated(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=False, ask_before_action=False)
        captured = []

        def fake_run_agent(agent_config, **kwargs):
            captured.append(agent_config)
            return _make_agent_result(True)

        with patch("psr_ai.core.replay._run_agent", side_effect=fake_run_agent):
            run_replay(config, echo_fn=lambda m: None)

        assert captured[0].dry_run is False

    def test_ask_before_action_false_by_default(self):
        config = ReplayConfig(zip_path=Path("x.zip"))
        assert config.ask_before_action is False

    def test_speed_affects_screenshot_interval(self, tmp_path):
        zip_path = _make_session_zip(tmp_path)
        config = ReplayConfig(zip_path=zip_path, dry_run=True, speed=2.0)
        captured = []

        def fake_run_agent(agent_config, **kwargs):
            captured.append(agent_config)
            return _make_agent_result(True)

        with patch("psr_ai.core.replay._run_agent", side_effect=fake_run_agent):
            run_replay(config, echo_fn=lambda m: None)

        assert captured[0].screenshot_interval == pytest.approx(1.0)
