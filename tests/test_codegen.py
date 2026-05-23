"""Tests for psr_ai.core.codegen."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _extract_steps_from_codegen
# ---------------------------------------------------------------------------

class TestExtractSteps:
    def test_parses_click_and_fill(self):
        from psr_ai.core.codegen import _extract_steps_from_codegen

        source = (
            "from playwright.sync_api import sync_playwright\n"
            "\n"
            "def test_recorded(page):\n"
            "    page.goto('https://example.com')\n"
            "    page.click('#submit')\n"
            "    page.fill('input[name=\"q\"]', 'hello')\n"
        )
        steps = _extract_steps_from_codegen(source, "https://example.com")
        actions = [s["action"] for s in steps]
        assert "goto" in actions
        assert "click" in actions
        assert "fill" in actions

    def test_empty_source_returns_empty(self):
        from psr_ai.core.codegen import _extract_steps_from_codegen

        steps = _extract_steps_from_codegen("", "https://example.com")
        assert steps == []

    def test_step_has_index(self):
        from psr_ai.core.codegen import _extract_steps_from_codegen

        source = "def test(page):\n    page.click('#a')\n    page.click('#b')\n"
        steps = _extract_steps_from_codegen(source, "https://example.com")
        assert steps[0]["index"] == 0
        assert steps[1]["index"] == 1


# ---------------------------------------------------------------------------
# record_actions_programmatic
# ---------------------------------------------------------------------------

class TestRecordActionsProgrammatic:
    def _make_playwright_ctx(self, page=None):
        if page is None:
            page = MagicMock()
            page.goto = MagicMock()
            page.click = MagicMock()
            page.fill = MagicMock()
            page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        browser = MagicMock()
        browser.new_page.return_value = page
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)
        return cm, page

    def test_click_action_in_output(self, tmp_path):
        from psr_ai.core.codegen import record_actions_programmatic

        cm, page = self._make_playwright_ctx()
        actions = [{"type": "click", "selector": "#submit"}]

        with patch("psr_ai.core.codegen.sync_playwright", return_value=cm):
            result = record_actions_programmatic(
                "https://example.com", actions,
                tmp_path / "test_out.py",
                output_dir=tmp_path,
            )

        assert result.output_file.exists()
        content = result.output_file.read_text()
        assert "page.click" in content

    def test_fill_action_recorded(self, tmp_path):
        from psr_ai.core.codegen import record_actions_programmatic

        cm, page = self._make_playwright_ctx()
        actions = [{"type": "fill", "selector": "input#q", "value": "hello"}]

        with patch("psr_ai.core.codegen.sync_playwright", return_value=cm):
            result = record_actions_programmatic(
                "https://example.com", actions,
                tmp_path / "test_out.py",
            )

        content = result.output_file.read_text()
        assert "page.fill" in content
        assert "hello" in content

    def test_data_json_saved_when_output_dir_given(self, tmp_path):
        from psr_ai.core.codegen import record_actions_programmatic

        cm, _ = self._make_playwright_ctx()
        actions = [{"type": "click", "selector": "#btn"}]

        with patch("psr_ai.core.codegen.sync_playwright", return_value=cm):
            result = record_actions_programmatic(
                "https://example.com", actions,
                tmp_path / "test_out.py",
                output_dir=tmp_path,
            )

        assert result.data_json is not None
        assert result.data_json.exists()
        data = json.loads(result.data_json.read_text())
        assert data["action_count"] == 1

    def test_action_count_matches(self, tmp_path):
        from psr_ai.core.codegen import record_actions_programmatic

        cm, _ = self._make_playwright_ctx()
        actions = [
            {"type": "click", "selector": "#a"},
            {"type": "click", "selector": "#b"},
            {"type": "click", "selector": "#c"},
        ]

        with patch("psr_ai.core.codegen.sync_playwright", return_value=cm):
            result = record_actions_programmatic(
                "https://example.com", actions,
                tmp_path / "test_out.py",
            )

        assert result.action_count == 3

    def test_raises_import_error_without_playwright(self, tmp_path):
        from psr_ai.core import codegen

        with patch.object(codegen, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                codegen.record_actions_programmatic(
                    "https://example.com", [],
                    tmp_path / "out.py",
                )

    def test_report_contains_url(self, tmp_path):
        from psr_ai.core.codegen import record_actions_programmatic

        cm, _ = self._make_playwright_ctx()

        with patch("psr_ai.core.codegen.sync_playwright", return_value=cm):
            result = record_actions_programmatic(
                "https://mysite.com", [],
                tmp_path / "test_out.py",
            )

        assert "https://mysite.com" in result.report()


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestCodegenCLI:
    def test_codegen_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["codegen", "--help"])
        assert result.exit_code == 0

    def test_codegen_help_mentions_browser(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["codegen", "--help"])
        assert "browser" in result.output.lower()
