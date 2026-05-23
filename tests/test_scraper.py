"""Tests for psr_ai.core.scraper."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_page(title="Test Title", html="<html><body>hello</body></html>", items=None):
    page = MagicMock()
    page.title.return_value = title
    page.content.return_value = html
    page.eval_on_selector_all.return_value = items or []
    page.evaluate.return_value = {"violations": [], "passes": 3}
    page.goto.return_value = MagicMock(status=200)
    return page


def _make_sync_playwright_ctx(page=None):
    """Return a context manager mock that yields a playwright-like object."""
    if page is None:
        page = _make_mock_page()
    browser = MagicMock()
    browser.new_page.return_value = page
    ctx_mock = MagicMock()
    ctx_mock.new_page.return_value = page
    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=playwright)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# browse_page
# ---------------------------------------------------------------------------

class TestBrowsePage:
    def test_saves_screenshot_and_html(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page(title="My Page", html="<html>content</html>")
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = browse_page("https://example.com", tmp_path)

        assert result.title == "My Page"
        assert result.screenshot.exists()
        assert result.html_file.exists()
        assert result.html_file.read_text() == "<html>content</html>"

    def test_result_contains_url_and_output_dir(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = browse_page("https://example.com", tmp_path)

        assert result.url == "https://example.com"
        assert result.output_dir == tmp_path

    def test_report_contains_title(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page(title="Demo Site")
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = browse_page("https://demo.com", tmp_path)

        assert "Demo Site" in result.report()

    def test_raises_import_error_without_playwright(self, tmp_path):
        from psr_ai.core import scraper

        with patch.object(scraper, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                scraper.browse_page("https://example.com", tmp_path)


# ---------------------------------------------------------------------------
# scrape_page
# ---------------------------------------------------------------------------

class TestScrapePage:
    def test_saves_json_with_items(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        items = [{"text": "Item 1", "html": "<li>Item 1</li>", "href": None, "src": None}]
        page = _make_mock_page(items=items)
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = scrape_page("https://example.com", "li", tmp_path)

        assert result.items == items
        assert len(result.items) == 1

        # JSON file must exist
        json_files = list(tmp_path.glob("*_scraped.json"))
        assert json_files
        saved = json.loads(json_files[0].read_text())
        assert saved == items

    def test_no_screenshot_flag(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        page = _make_mock_page(items=[])

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = scrape_page("https://example.com", "li", tmp_path, screenshot=False)

        assert result.screenshot is None

    def test_report_mentions_selector(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        page = _make_mock_page(items=[{"text": "a", "html": "", "href": None, "src": None}])
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = scrape_page("https://example.com", ".article", tmp_path)

        assert ".article" in result.report()


# ---------------------------------------------------------------------------
# intercept_network
# ---------------------------------------------------------------------------

class TestInterceptNetwork:
    def test_logs_requests_to_json(self, tmp_path):
        from psr_ai.core.scraper import intercept_network

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        page.on = MagicMock()
        page.goto = MagicMock(return_value=MagicMock(status=200))

        browser = MagicMock()
        browser.new_page.return_value = page
        ctx = MagicMock()
        ctx.new_page.return_value = page
        ctx.route = MagicMock()
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        browser.new_context.return_value = ctx
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = intercept_network("https://example.com", tmp_path)

        log_files = list(tmp_path.glob("*_network.json"))
        assert log_files
        assert result.requests_file.exists()

    def test_result_has_output_dir(self, tmp_path):
        from psr_ai.core.scraper import intercept_network

        page = _make_mock_page()
        page.on = MagicMock()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        browser = MagicMock()
        ctx = MagicMock()
        ctx.new_page.return_value = page
        ctx.route = MagicMock()
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        browser.new_context.return_value = ctx
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = intercept_network("https://example.com", tmp_path)

        assert result.output_dir == tmp_path


# ---------------------------------------------------------------------------
# run_accessibility_audit
# ---------------------------------------------------------------------------

class TestAccessibilityAudit:
    def test_no_violations_report(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        page = _make_mock_page()
        page.evaluate.return_value = {"violations": [], "passes": 5}
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = run_accessibility_audit("https://example.com", tmp_path)

        assert result.violations == []
        assert result.passes == 5
        assert result.report_file.exists()

    def test_violations_appear_in_report(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        violations = [{"rule": "image-alt", "description": "Images must have alt text", "element": "<img>", "selector": "img"}]
        page = _make_mock_page()
        page.evaluate.return_value = {"violations": violations, "passes": 2}

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = run_accessibility_audit("https://example.com", tmp_path)

        assert len(result.violations) == 1
        report_text = result.report_file.read_text()
        assert "image-alt" in report_text

    def test_json_file_saved(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        page = _make_mock_page()
        page.evaluate.return_value = {"violations": [], "passes": 0}

        with patch("psr_ai.core.scraper.sync_playwright", return_value=_make_sync_playwright_ctx(page)):
            result = run_accessibility_audit("https://example.com", tmp_path)

        json_files = list(tmp_path.glob("*_accessibility.json"))
        assert json_files


# ---------------------------------------------------------------------------
# capture_trace
# ---------------------------------------------------------------------------

class TestCaptureTrace:
    def test_creates_trace_zip(self, tmp_path):
        from psr_ai.core.scraper import capture_trace

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        ctx = MagicMock()
        ctx.new_page.return_value = page
        ctx.tracing = MagicMock()
        ctx.tracing.stop = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"TRACE"))

        browser = MagicMock()
        browser.new_context.return_value = ctx
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = capture_trace("https://example.com", tmp_path)

        assert result.trace_file.name.endswith("_trace.zip")
        assert result.trace_file.exists()

    def test_report_mentions_trace_show(self, tmp_path):
        from psr_ai.core.scraper import capture_trace

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))

        ctx = MagicMock()
        ctx.new_page.return_value = page
        ctx.tracing = MagicMock()
        ctx.tracing.stop = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"TRACE"))

        browser = MagicMock()
        browser.new_context.return_value = ctx
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = capture_trace("https://example.com", tmp_path)

        assert "trace-show" in result.report()


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestScraperCLI:
    def test_browse_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["browse", "--help"])
        assert result.exit_code == 0
        assert "screenshot" in result.output.lower() or "browse" in result.output.lower()

    def test_scrape_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scrape", "--help"])
        assert result.exit_code == 0

    def test_intercept_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["intercept", "--help"])
        assert result.exit_code == 0

    def test_accessibility_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["accessibility", "--help"])
        assert result.exit_code == 0

    def test_trace_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["trace", "--help"])
        assert result.exit_code == 0

    def test_test_all_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["test-all", "--help"])
        assert result.exit_code == 0
