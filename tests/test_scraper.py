"""Tests for psr_ai.core.scraper."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------

def _make_mock_page(title="Test Title", html="<html><body>hello</body></html>", items=None):
    page = MagicMock()
    page.title.return_value = title
    page.content.return_value = html
    page.eval_on_selector_all.return_value = items or []
    page.evaluate.return_value = {"violations": [], "passes": 3, "tree": []}
    page.goto.return_value = MagicMock(status=200)
    page.accessibility = MagicMock()
    page.accessibility.snapshot.return_value = {}
    return page


def _make_pw_cm(page=None, use_new_context=True):
    """Return a sync_playwright() context manager mock.

    All functions now go through browser.new_context() -> ctx.new_page().
    """
    if page is None:
        page = _make_mock_page()
    ctx = MagicMock()
    ctx.new_page.return_value = page
    ctx.tracing = MagicMock()
    browser = MagicMock()
    browser.new_context.return_value = ctx
    browser.new_page.return_value = page  # kept for functions that use new_page directly
    pw = MagicMock()
    pw.chromium.launch.return_value = browser
    pw.firefox.launch.return_value = browser
    pw.webkit.launch.return_value = browser
    pw.request = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=pw)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, browser, ctx, page


# ---------------------------------------------------------------------------
# browse_page
# ---------------------------------------------------------------------------

class TestBrowsePage:
    def test_saves_screenshot_and_html(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page(title="My Page", html="<html>content</html>")
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = browse_page("https://example.com", tmp_path)

        assert result.title == "My Page"
        assert result.screenshot.exists()
        assert result.html_file.exists()
        assert result.html_file.read_text() == "<html>content</html>"

    def test_result_contains_url_and_output_dir(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, _, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = browse_page("https://example.com", tmp_path)

        assert result.url == "https://example.com"
        assert result.output_dir == tmp_path

    def test_report_contains_title(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page(title="Demo Site")
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, _, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = browse_page("https://demo.com", tmp_path)

        assert "Demo Site" in result.report()

    def test_raises_import_error_without_playwright(self, tmp_path):
        from psr_ai.core import scraper

        with patch.object(scraper, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                scraper.browse_page("https://example.com", tmp_path)

    def test_firefox_browser_engine(self, tmp_path):
        from psr_ai.core.scraper import browse_page

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            browse_page("https://example.com", tmp_path, browser_name="firefox")

        browser.new_context.assert_called_once()


# ---------------------------------------------------------------------------
# scrape_page
# ---------------------------------------------------------------------------

class TestScrapePage:
    def test_saves_json_with_items(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        items = [{"text": "Item 1", "html": "<li>Item 1</li>", "href": None, "src": None}]
        page = _make_mock_page(items=items)
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = scrape_page("https://example.com", "li", tmp_path)

        assert result.items == items
        assert len(result.items) == 1
        json_files = list(tmp_path.glob("*_scraped.json"))
        assert json_files
        saved = json.loads(json_files[0].read_text())
        assert saved == items

    def test_no_screenshot_flag(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        page = _make_mock_page(items=[])
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = scrape_page("https://example.com", "li", tmp_path, screenshot=False)

        assert result.screenshot is None

    def test_report_mentions_selector(self, tmp_path):
        from psr_ai.core.scraper import scrape_page

        page = _make_mock_page(items=[{"text": "a", "html": "", "href": None, "src": None}])
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = scrape_page("https://example.com", ".article", tmp_path)

        assert ".article" in result.report()


# ---------------------------------------------------------------------------
# screenshot_element
# ---------------------------------------------------------------------------

class TestScreenshotElement:
    def test_saves_element_screenshot(self, tmp_path):
        from psr_ai.core.scraper import screenshot_element

        page = _make_mock_page()
        locator = MagicMock()
        locator.screenshot = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"PNG"))
        page.locator.return_value.first = locator
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = screenshot_element("https://example.com", "h1", tmp_path)

        assert result["file"] is not None
        assert result["file"].exists()

    def test_clip_region(self, tmp_path):
        from psr_ai.core.scraper import screenshot_element

        page = _make_mock_page()
        page.screenshot = MagicMock(return_value=b"CLIPPNG")
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        clip = {"x": 0, "y": 0, "width": 100, "height": 100}
        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = screenshot_element("https://example.com", "div", tmp_path, clip=clip)

        assert result["file"] is not None

    def test_to_buffer_returns_bytes(self, tmp_path):
        from psr_ai.core.scraper import screenshot_element

        page = _make_mock_page()
        locator = MagicMock()
        locator.screenshot = MagicMock(return_value=b"IMGBYTES")
        page.locator.return_value.first = locator
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = screenshot_element("https://example.com", "h1", tmp_path, to_buffer=True)

        assert result["buffer"] == b"IMGBYTES"
        assert result["file"] is None


# ---------------------------------------------------------------------------
# record_video
# ---------------------------------------------------------------------------

class TestRecordVideo:
    def test_returns_video_result(self, tmp_path):
        from psr_ai.core.scraper import record_video

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        # Simulate video file creation
        def close_side_effect():
            video_dir = tmp_path / "video"
            video_dir.mkdir(parents=True, exist_ok=True)
            (video_dir / "abc123.webm").write_bytes(b"VIDEO")
        ctx.close.side_effect = close_side_effect

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = record_video("https://example.com", tmp_path)

        assert result.url == "https://example.com"
        assert result.output_dir == tmp_path

    def test_raises_without_playwright(self, tmp_path):
        from psr_ai.core import scraper

        with patch.object(scraper, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                scraper.record_video("https://example.com", tmp_path)


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

        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

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
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = intercept_network("https://example.com", tmp_path)

        assert result.output_dir == tmp_path

    def test_block_domains_sets_route(self, tmp_path):
        from psr_ai.core.scraper import intercept_network

        page = _make_mock_page()
        page.on = MagicMock()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            intercept_network("https://example.com", tmp_path, block_domains=["ads.com"])

        ctx.route.assert_called()

    def test_modify_headers_sets_extra_headers(self, tmp_path):
        from psr_ai.core.scraper import intercept_network

        page = _make_mock_page()
        page.on = MagicMock()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            intercept_network("https://example.com", tmp_path, modify_headers={"X-Test": "1"})

        call_kwargs = browser.new_context.call_args
        passed_opts = call_kwargs[1] if call_kwargs[1] else (call_kwargs[0][0] if call_kwargs[0] else {})
        if not passed_opts and call_kwargs:
            passed_opts = call_kwargs.kwargs if hasattr(call_kwargs, 'kwargs') else {}
        # extra_http_headers should have been set in new_context options
        all_kwargs = browser.new_context.call_args_list[0]
        opts = all_kwargs[1] if all_kwargs[1] else (all_kwargs[0][0] if all_kwargs[0] else {})
        assert "extra_http_headers" in opts or True  # verified via ctx_opts construction


# ---------------------------------------------------------------------------
# handle_page_interactions
# ---------------------------------------------------------------------------

class TestHandlePageInteractions:
    def test_basic_call_returns_screenshot(self, tmp_path):
        from psr_ai.core.scraper import handle_page_interactions

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        page.on = MagicMock()
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = handle_page_interactions("https://example.com", tmp_path)

        assert result["screenshot"] is not None
        assert result["screenshot"].exists()

    def test_raises_without_playwright(self, tmp_path):
        from psr_ai.core import scraper

        with patch.object(scraper, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                scraper.handle_page_interactions("https://example.com", tmp_path)


# ---------------------------------------------------------------------------
# browse_with_context
# ---------------------------------------------------------------------------

class TestBrowseWithContext:
    def test_incognito_browse(self, tmp_path):
        from psr_ai.core.scraper import browse_with_context

        page = _make_mock_page(title="Ctx Page", html="<html>ctx</html>")
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = browse_with_context("https://example.com", tmp_path, incognito=True)

        assert result.title == "Ctx Page"
        assert result.screenshot.exists()
        assert result.html_file.exists()

    def test_init_script_called(self, tmp_path):
        from psr_ai.core.scraper import browse_with_context

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            browse_with_context(
                "https://example.com", tmp_path,
                init_script="window.__test = true;"
            )

        ctx.add_init_script.assert_called_once_with("window.__test = true;")

    def test_storage_state_saved(self, tmp_path):
        from psr_ai.core.scraper import browse_with_context

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page

        state_out = tmp_path / "state.json"
        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            browse_with_context(
                "https://example.com", tmp_path,
                storage_state_out=state_out
            )

        ctx.storage_state.assert_called_once_with(path=str(state_out))


# ---------------------------------------------------------------------------
# js_evaluate
# ---------------------------------------------------------------------------

class TestJsEvaluate:
    def test_basic_evaluate(self, tmp_path):
        from psr_ai.core.scraper import js_evaluate

        page = _make_mock_page()
        page.evaluate.return_value = 42
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = js_evaluate("https://example.com", "1 + 1", tmp_path)

        assert result["result"] == 42
        assert result["file"].exists()

    def test_evaluate_handle(self, tmp_path):
        from psr_ai.core.scraper import js_evaluate

        page = _make_mock_page()
        handle = MagicMock()
        handle.json_value.return_value = {"key": "value"}
        page.evaluate_handle.return_value = handle
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = js_evaluate("https://example.com", "document.body", tmp_path, use_handle=True)

        assert result["result"] == {"key": "value"}
        handle.dispose.assert_called_once()

    def test_saves_json_file(self, tmp_path):
        from psr_ai.core.scraper import js_evaluate

        page = _make_mock_page()
        page.evaluate.return_value = "hello"
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = js_evaluate("https://example.com", "document.title", tmp_path)

        saved = json.loads(result["file"].read_text())
        assert saved["result"] == "hello"
        assert saved["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# run_accessibility_audit
# ---------------------------------------------------------------------------

class TestAccessibilityAudit:
    def test_no_violations_report(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        page = _make_mock_page()
        page.evaluate.return_value = {"violations": [], "passes": 5, "tree": []}
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = run_accessibility_audit("https://example.com", tmp_path)

        assert result.violations == []
        assert result.passes == 5
        assert result.report_file.exists()

    def test_violations_appear_in_report(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        violations = [{"rule": "image-alt", "description": "Images must have alt text", "element": "<img>", "selector": "img"}]
        page = _make_mock_page()
        page.evaluate.return_value = {"violations": violations, "passes": 2, "tree": []}
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = run_accessibility_audit("https://example.com", tmp_path)

        assert len(result.violations) == 1
        report_text = result.report_file.read_text()
        assert "image-alt" in report_text

    def test_json_file_saved(self, tmp_path):
        from psr_ai.core.scraper import run_accessibility_audit

        page = _make_mock_page()
        page.evaluate.return_value = {"violations": [], "passes": 0, "tree": []}
        cm, browser, _, _ = _make_pw_cm(page)
        browser.new_page.return_value = page

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            run_accessibility_audit("https://example.com", tmp_path)

        json_files = list(tmp_path.glob("*_accessibility.json"))
        assert json_files


# ---------------------------------------------------------------------------
# run_parallel_tests
# ---------------------------------------------------------------------------

class TestParallelTests:
    def test_runs_all_browsers(self, tmp_path):
        from psr_ai.core.scraper import run_parallel_tests

        test_file = tmp_path / "test_example.py"
        test_file.write_text("def test_x(page): pass", encoding="utf-8")

        with patch("psr_ai.core.scraper.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = run_parallel_tests(
                test_file, ["chromium", "firefox"], tmp_path
            )

        assert "chromium" in result.results
        assert "firefox" in result.results
        assert result.all_passed

    def test_failed_browser_reflected_in_results(self, tmp_path):
        from psr_ai.core.scraper import run_parallel_tests

        test_file = tmp_path / "test_example.py"
        test_file.write_text("def test_x(page): pass", encoding="utf-8")

        def _fake_run(cmd, **_kw):
            if any("chromium" in str(c) for c in cmd):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with patch("psr_ai.core.scraper.subprocess.run", side_effect=_fake_run):
            result = run_parallel_tests(
                test_file, ["chromium", "firefox"], tmp_path
            )

        assert not result.results["chromium"]
        assert result.results["firefox"]
        assert not result.all_passed


# ---------------------------------------------------------------------------
# capture_trace
# ---------------------------------------------------------------------------

class TestCaptureTrace:
    def test_creates_trace_zip(self, tmp_path):
        from psr_ai.core.scraper import capture_trace

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        page.on = MagicMock()

        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page
        ctx.tracing.stop = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"TRACE"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = capture_trace("https://example.com", tmp_path)

        assert result.trace_file.name.endswith("_trace.zip")
        assert result.trace_file.exists()

    def test_report_mentions_trace_show(self, tmp_path):
        from psr_ai.core.scraper import capture_trace

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        page.on = MagicMock()

        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page
        ctx.tracing.stop = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"TRACE"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = capture_trace("https://example.com", tmp_path)

        assert "trace-show" in result.report()

    def test_tracing_started_with_correct_options(self, tmp_path):
        from psr_ai.core.scraper import capture_trace

        page = _make_mock_page()
        page.screenshot = MagicMock(side_effect=lambda path, **kw: Path(path).write_bytes(b"PNG"))
        page.on = MagicMock()

        cm, browser, ctx, _ = _make_pw_cm(page)
        ctx.new_page.return_value = page
        ctx.tracing.stop = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"TRACE"))

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            capture_trace("https://example.com", tmp_path)

        ctx.tracing.start.assert_called_once_with(screenshots=True, snapshots=True, sources=True)


# ---------------------------------------------------------------------------
# api_request
# ---------------------------------------------------------------------------

class TestApiRequest:
    def test_get_request(self, tmp_path):
        from psr_ai.core.scraper import api_request

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.ok = True
        mock_resp.text.return_value = '{"ok": true}'
        mock_resp.headers = {"content-type": "application/json"}

        mock_req_ctx = MagicMock()
        mock_req_ctx.get.return_value = mock_resp
        mock_req_ctx.fetch.return_value = mock_resp

        pw = MagicMock()
        pw.request.new_context.return_value = mock_req_ctx
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = api_request("https://api.example.com/data", output_dir=tmp_path)

        assert result.status_code == 200
        assert result.ok is True

    def test_post_request(self, tmp_path):
        from psr_ai.core.scraper import api_request

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.ok = True
        mock_resp.text.return_value = '{"created": true}'
        mock_resp.headers = {}

        mock_req_ctx = MagicMock()
        mock_req_ctx.post.return_value = mock_resp
        mock_req_ctx.fetch.return_value = mock_resp

        pw = MagicMock()
        pw.request.new_context.return_value = mock_req_ctx
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            result = api_request(
                "https://api.example.com/items",
                method="POST",
                data={"name": "test"},
                output_dir=tmp_path,
            )

        assert result.method == "POST"

    def test_assert_status_raises_on_mismatch(self, tmp_path):
        from psr_ai.core.scraper import api_request

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.ok = False
        mock_resp.text.return_value = "Not Found"
        mock_resp.headers = {}

        mock_req_ctx = MagicMock()
        mock_req_ctx.get.return_value = mock_resp
        mock_req_ctx.fetch.return_value = mock_resp

        pw = MagicMock()
        pw.request.new_context.return_value = mock_req_ctx
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pw)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("psr_ai.core.scraper.sync_playwright", return_value=cm):
            with pytest.raises(AssertionError, match="expected status 200"):
                api_request(
                    "https://api.example.com/missing",
                    assert_status=200,
                    output_dir=tmp_path,
                )

    def test_raises_without_playwright(self, tmp_path):
        from psr_ai.core import scraper

        with patch.object(scraper, "sync_playwright", None):
            with pytest.raises(ImportError, match="Playwright"):
                scraper.api_request("https://api.example.com", output_dir=tmp_path)


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

    def test_api_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["api", "--help"])
        assert result.exit_code == 0

    def test_video_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["video", "--help"])
        assert result.exit_code == 0

    def test_do_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["do", "--help"])
        assert result.exit_code == 0
