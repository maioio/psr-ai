"""Unit tests for core/testgen.py - Playwright test generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psr_ai.core.testgen import (
    generate_playwright_test,
    GenerateTestResult,
    _group_by_program,
    _safe_identifier,
    _action_to_playwright,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Path, steps: list[dict] | None = None) -> Path:
    session = tmp_path / "session_output"
    session.mkdir(exist_ok=True)
    if steps is None:
        steps = [
            {
                "index": 0,
                "label": "Open page",
                "timestamp": "1/15/2024 2:30:00 PM",
                "program": "Chrome",
                "action_text": "navigate to http://localhost:3000",
                "screenshot": "screenshots/step_000.png",
                "click_x": 0,
                "click_y": 0,
            },
            {
                "index": 1,
                "label": "Click button",
                "timestamp": "1/15/2024 2:30:05 PM",
                "program": "Chrome",
                "action_text": "Click Save button",
                "screenshot": "screenshots/step_001.png",
                "click_x": 400,
                "click_y": 300,
            },
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
# generate_playwright_test
# ---------------------------------------------------------------------------

class TestGeneratePlaywrightTest:

    def test_creates_output_file(self, tmp_path):
        session = _make_session(tmp_path)
        out = tmp_path / "out" / "test_generated.py"
        result = generate_playwright_test(session, output_path=out)
        assert out.exists()

    def test_returns_test_gen_result(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        assert isinstance(result, GenerateTestResult)

    def test_result_step_count(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        assert result.step_count == 2

    def test_output_is_valid_python(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        compile(code, str(result.output_path), "exec")

    def test_generated_file_imports_playwright(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "from playwright.sync_api import" in code

    def test_generated_file_has_test_functions(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "def test_" in code

    def test_raises_when_data_json_missing(self, tmp_path):
        empty = tmp_path / "empty_session"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            generate_playwright_test(empty)

    def test_raises_for_unsupported_framework(self, tmp_path):
        session = _make_session(tmp_path)
        with pytest.raises(ValueError, match="Unsupported framework"):
            generate_playwright_test(session, framework="selenium")

    def test_default_output_path_is_generated_test_py(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        assert result.output_path == session / "generated_test.py"

    def test_empty_session_produces_placeholder(self, tmp_path):
        session = _make_session(tmp_path, steps=[])
        result = generate_playwright_test(session)
        assert result.step_count == 0
        assert result.generated_test_count >= 1  # placeholder
        assert result.warnings

    def test_navigate_action_produces_goto(self, tmp_path):
        session = _make_session(tmp_path, steps=[{
            "index": 0, "label": "Go", "timestamp": "", "program": "Chrome",
            "action_text": "navigate to http://example.com",
            "screenshot": "", "click_x": 0, "click_y": 0,
        }])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "page.goto(" in code

    def test_click_with_coords_produces_mouse_click(self, tmp_path):
        session = _make_session(tmp_path, steps=[{
            "index": 0, "label": "Click", "timestamp": "", "program": "Chrome",
            "action_text": "click",
            "screenshot": "", "click_x": 200, "click_y": 150,
        }])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "page.mouse.click(200, 150)" in code

    def test_scroll_action_produces_mouse_wheel(self, tmp_path):
        session = _make_session(tmp_path, steps=[{
            "index": 0, "label": "Scroll", "timestamp": "", "program": "Chrome",
            "action_text": "scroll down",
            "screenshot": "", "click_x": 0, "click_y": 0,
        }])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "page.mouse.wheel" in code

    def test_type_action_produces_keyboard_type(self, tmp_path):
        session = _make_session(tmp_path, steps=[{
            "index": 0, "label": "Type", "timestamp": "", "program": "Chrome",
            "action_text": "type hello world",
            "screenshot": "", "click_x": 0, "click_y": 0,
        }])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "page.keyboard.type" in code

    def test_ai_description_appears_in_comment(self, tmp_path):
        session = _make_session(tmp_path, steps=[{
            "index": 0, "label": "Step 0", "timestamp": "1/1/2024 1:00:00 PM",
            "program": "Chrome", "action_text": "click",
            "screenshot": "", "click_x": 0, "click_y": 0,
            "ai_description": "User clicked the Submit button to confirm the form.",
        }])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "Submit button" in code

    def test_multiple_programs_produce_multiple_test_functions(self, tmp_path):
        session = _make_session(tmp_path, steps=[
            {"index": 0, "label": "A", "timestamp": "", "program": "Chrome",
             "action_text": "click", "screenshot": "", "click_x": 1, "click_y": 1},
            {"index": 1, "label": "B", "timestamp": "", "program": "Notepad",
             "action_text": "click", "screenshot": "", "click_x": 2, "click_y": 2},
        ])
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert code.count("def test_") >= 2

    def test_zero_em_dashes_in_generated_file(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_playwright_test(session)
        code = result.output_path.read_text(encoding="utf-8")
        assert "—" not in code


# ---------------------------------------------------------------------------
# _group_by_program
# ---------------------------------------------------------------------------

class TestGroupByProgram:

    def test_same_program_in_one_group(self):
        steps = [
            {"index": 0, "program": "Chrome", "action_text": "a"},
            {"index": 1, "program": "Chrome", "action_text": "b"},
        ]
        groups = _group_by_program(steps)
        assert len(groups) == 1
        assert groups[0][0] == "Chrome"
        assert len(groups[0][1]) == 2

    def test_different_programs_in_separate_groups(self):
        steps = [
            {"index": 0, "program": "Chrome"},
            {"index": 1, "program": "Notepad"},
        ]
        groups = _group_by_program(steps)
        assert len(groups) == 2

    def test_empty_steps_returns_empty_list(self):
        assert _group_by_program([]) == []

    def test_program_change_and_back_creates_three_groups(self):
        steps = [
            {"index": 0, "program": "A"},
            {"index": 1, "program": "B"},
            {"index": 2, "program": "A"},
        ]
        groups = _group_by_program(steps)
        assert len(groups) == 3


# ---------------------------------------------------------------------------
# _safe_identifier
# ---------------------------------------------------------------------------

class TestSafeIdentifier:

    def test_lowercases_and_replaces_spaces(self):
        assert _safe_identifier("My App") == "my_app"

    def test_strips_leading_digits(self):
        ident = _safe_identifier("3d render")
        assert not ident[0].isdigit()

    def test_collapses_multiple_underscores(self):
        result = _safe_identifier("foo   bar")
        assert "__" not in result

    def test_truncates_to_40_chars(self):
        result = _safe_identifier("a" * 100)
        assert len(result) <= 40


# ---------------------------------------------------------------------------
# _action_to_playwright
# ---------------------------------------------------------------------------

class TestActionToPlaywright:

    def test_navigate_returns_goto_with_url(self):
        code = _action_to_playwright(
            "navigate to http://localhost:3000", "label", None, None, "", []
        )
        assert "page.goto" in code
        assert "http://localhost:3000" in code

    def test_click_with_coords_returns_mouse_click(self):
        code = _action_to_playwright("click button", "Save", 100, 200, "", [])
        assert "page.mouse.click(100, 200)" in code

    def test_scroll_returns_mouse_wheel(self):
        code = _action_to_playwright("scroll down", "label", None, None, "", [])
        assert "page.mouse.wheel" in code

    def test_type_returns_keyboard_type(self):
        code = _action_to_playwright("type hello", "label", None, None, "", [])
        assert "page.keyboard.type" in code

    def test_screenshot_returns_expect_screenshot(self):
        code = _action_to_playwright("screenshot", "label", None, None, "screenshots/step_000.png", [])
        assert "to_have_screenshot" in code

    def test_unknown_action_returns_empty_string(self):
        code = _action_to_playwright("some unknown action xyz", "label", None, None, "", [])
        assert code == ""
