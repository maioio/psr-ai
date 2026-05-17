"""Unit tests for core/healer.py - self-healing test pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from psr_ai.core.healer import (
    HealResult,
    FailingTest,
    heal,
    _load_session_context,
    _parse_totals,
    _parse_failures,
    _run_pytest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_session_dir(tmp_path: Path, steps: list[dict] | None = None) -> Path:
    session = tmp_path / "session_output"
    session.mkdir()
    if steps is None:
        steps = [
            {
                "index": 0,
                "label": "Step 0",
                "timestamp": "1/15/2024 2:30:00 PM",
                "program": "Chrome",
                "action_text": "Clicked Save",
                "screenshot": "",
            }
        ]
    data = {
        "title": "Test Session",
        "source": "test.zip",
        "generated_at": "2024-01-15T00:00:00+00:00",
        "step_count": len(steps),
        "steps": steps,
    }
    (session / "data.json").write_text(json.dumps(data), encoding="utf-8")
    return session


# ---------------------------------------------------------------------------
# _load_session_context
# ---------------------------------------------------------------------------

class TestLoadSessionContext:

    def test_returns_last_step_fields(self, tmp_path):
        session = _make_session_dir(tmp_path)
        ctx = _load_session_context(session)
        assert ctx["last_step_program"] == "Chrome"
        assert ctx["last_step_action"] == "Clicked Save"

    def test_returns_empty_when_data_json_missing(self, tmp_path):
        ctx = _load_session_context(tmp_path / "nonexistent")
        assert ctx == {}

    def test_returns_empty_when_steps_list_is_empty(self, tmp_path):
        session = _make_session_dir(tmp_path, steps=[])
        ctx = _load_session_context(session)
        assert ctx["step_count"] == 0
        assert ctx["last_step_action"] == ""

    def test_includes_session_title(self, tmp_path):
        session = _make_session_dir(tmp_path)
        ctx = _load_session_context(session)
        assert ctx["session_title"] == "Test Session"

    def test_handles_malformed_json_gracefully(self, tmp_path):
        session = tmp_path / "bad"
        session.mkdir()
        (session / "data.json").write_bytes(b"\x00\x01 not json")
        ctx = _load_session_context(session)
        assert ctx == {}


# ---------------------------------------------------------------------------
# _parse_totals
# ---------------------------------------------------------------------------

class TestParseTotals:

    def test_parses_all_passing(self):
        output = "collected 50 items\n50 passed in 1.23s"
        t = _parse_totals(output)
        assert t["passed"] == 50
        assert t["failed"] == 0

    def test_parses_mixed_result(self):
        output = "collected 30 items\n3 failed, 27 passed in 0.45s"
        t = _parse_totals(output)
        assert t["failed"] == 3
        assert t["passed"] == 27

    def test_parses_error_count(self):
        output = "1 error, 2 failed, 10 passed in 0.5s"
        t = _parse_totals(output)
        assert t["errors"] == 1
        assert t["failed"] == 2

    def test_returns_zeros_on_empty_output(self):
        t = _parse_totals("")
        assert t["passed"] == 0
        assert t["failed"] == 0


# ---------------------------------------------------------------------------
# _parse_failures
# ---------------------------------------------------------------------------

class TestParseFailures:

    def test_extracts_failed_node_ids(self, tmp_path):
        output = (
            "FAILED tests/test_parser.py::test_extract_zip_valid - AssertionError: expected True\n"
            "FAILED tests/test_generator.py::test_render - ValueError: no steps\n"
        )
        failures = _parse_failures(output, tmp_path)
        node_ids = [f.nodeid for f in failures]
        assert "tests/test_parser.py::test_extract_zip_valid" in node_ids
        assert "tests/test_generator.py::test_render" in node_ids

    def test_extracts_short_summary(self, tmp_path):
        output = "FAILED tests/test_parser.py::test_foo - AssertionError: path mismatch\n"
        failures = _parse_failures(output, tmp_path)
        assert failures[0].short_summary == "AssertionError: path mismatch"

    def test_empty_output_returns_empty_list(self, tmp_path):
        assert _parse_failures("", tmp_path) == []

    def test_summary_truncated_at_200_chars(self, tmp_path):
        long_msg = "A" * 300
        output = f"FAILED tests/test_foo.py::test_bar - {long_msg}\n"
        failures = _parse_failures(output, tmp_path)
        assert len(failures[0].short_summary) <= 200


# ---------------------------------------------------------------------------
# heal() integration
# ---------------------------------------------------------------------------

class TestHeal:

    def _green_pytest_result(self):
        return ("50 passed in 1.23s\ncollected 50 items\n", 0)

    def _red_pytest_result(self):
        output = (
            "collected 50 items\n"
            "FAILED tests/test_parser.py::test_zip - AssertionError: mismatch\n"
            "2 failed, 48 passed in 1.5s\n"
        )
        return (output, 1)

    def test_all_green_sets_success(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._green_pytest_result()):
            result = heal(project_root=tmp_path)
        assert result.all_green is True
        assert result.returncode == 0

    def test_failing_tests_sets_all_green_false(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._red_pytest_result()):
            result = heal(project_root=tmp_path)
        assert result.all_green is False
        assert result.failed == 2

    def test_session_context_included_when_provided(self, tmp_path):
        session = _make_session_dir(tmp_path)
        with patch("psr_ai.core.healer._run_pytest", return_value=self._green_pytest_result()):
            result = heal(project_root=tmp_path, session_dir=session)
        assert result.session_context["last_step_program"] == "Chrome"

    def test_session_context_empty_when_not_provided(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._green_pytest_result()):
            result = heal(project_root=tmp_path)
        assert result.session_context == {}

    def test_failing_tests_populated(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._red_pytest_result()):
            result = heal(project_root=tmp_path)
        assert len(result.failing_tests) >= 1
        assert "test_zip" in result.failing_tests[0].nodeid

    def test_max_failures_cap(self, tmp_path):
        lines = "\n".join(
            f"FAILED tests/test_x.py::test_{i} - Error {i}" for i in range(30)
        )
        output = f"collected 30 items\n{lines}\n30 failed in 1s\n"
        with patch("psr_ai.core.healer._run_pytest", return_value=(output, 1)):
            result = heal(project_root=tmp_path, max_failures=5)
        assert len(result.failing_tests) <= 5

    def test_report_contains_project_root(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._green_pytest_result()):
            result = heal(project_root=tmp_path)
        assert str(tmp_path) in result.report()

    def test_report_contains_green_message_when_passing(self, tmp_path):
        with patch("psr_ai.core.healer._run_pytest", return_value=self._green_pytest_result()):
            result = heal(project_root=tmp_path)
        assert "All tests pass" in result.report()

    def test_heal_propagates_unexpected_exceptions(self, tmp_path):
        """_run_pytest failures bubble up; the CLI wrapper owns the exception boundary."""
        with patch("psr_ai.core.healer._run_pytest", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                heal(project_root=tmp_path)
