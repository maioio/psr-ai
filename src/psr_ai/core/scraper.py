"""Browser scraping, accessibility auditing, network interception, parallel testing, and trace capture."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    url: str
    selector: str | None
    output_dir: Path
    items: list[dict]
    screenshot: Path | None = None
    html_file: Path | None = None

    def report(self) -> str:
        parts = [f"Scraped: {self.url}"]
        if self.selector:
            parts.append(f"  Selector: {self.selector}")
        parts.append(f"  Items: {len(self.items)}")
        if self.screenshot:
            parts.append(f"  Screenshot: {self.screenshot}")
        if self.html_file:
            parts.append(f"  HTML: {self.html_file}")
        return "\n".join(parts)


@dataclass
class BrowseResult:
    url: str
    output_dir: Path
    screenshot: Path
    html_file: Path
    title: str = ""

    def report(self) -> str:
        lines = [f"Browsed: {self.url}"]
        if self.title:
            lines.append(f"  Title: {self.title}")
        lines.append(f"  Screenshot: {self.screenshot}")
        lines.append(f"  HTML: {self.html_file}")
        return "\n".join(lines)


@dataclass
class InterceptResult:
    url: str
    output_dir: Path
    requests_file: Path
    screenshot: Path | None = None
    request_count: int = 0
    blocked_count: int = 0

    def report(self) -> str:
        lines = [f"Intercepted: {self.url}"]
        lines.append(f"  Requests logged: {self.request_count}")
        if self.blocked_count:
            lines.append(f"  Requests blocked: {self.blocked_count}")
        lines.append(f"  Log: {self.requests_file}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        return "\n".join(lines)


@dataclass
class AccessibilityResult:
    url: str
    output_dir: Path
    report_file: Path
    violations: list[dict] = field(default_factory=list)
    passes: int = 0

    def report(self) -> str:
        lines = [f"Accessibility audit: {self.url}"]
        lines.append(f"  Violations: {len(self.violations)}")
        lines.append(f"  Passes: {self.passes}")
        lines.append(f"  Report: {self.report_file}")
        return "\n".join(lines)


@dataclass
class ParallelTestResult:
    test_file: Path
    browsers: list[str]
    output_dir: Path
    results: dict[str, bool] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return all(self.results.values())

    def report(self) -> str:
        lines = ["Parallel browser test results:"]
        for browser, passed in self.results.items():
            status = "PASS" if passed else "FAIL"
            lines.append(f"  {browser}: {status}")
        lines.append(f"  Reports: {self.output_dir}")
        return "\n".join(lines)


@dataclass
class TraceResult:
    url: str
    output_dir: Path
    trace_file: Path
    screenshot: Path | None = None

    def report(self) -> str:
        lines = [f"Trace captured: {self.url}"]
        lines.append(f"  Trace: {self.trace_file}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        lines.append("  View with: sa trace-show <trace.zip>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_playwright():
    if sync_playwright is None:
        raise ImportError(
            "Playwright is required for this command.\n"
            "Run: pip install psr-ai[capture]"
        )


def _safe_filename(url: str, max_len: int = 60) -> str:
    stem = url.split("//")[-1].replace("/", "_").strip("_")
    return re.sub(r"[^\w\-.]", "_", stem)[:max_len]


# ---------------------------------------------------------------------------
# browse_page
# ---------------------------------------------------------------------------

def browse_page(
    url: str,
    output_dir: Path,
    wait_for: str | None = None,
    delay: float = 1.5,
) -> BrowseResult:
    """Full-page screenshot + raw HTML capture of a URL."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    screenshot_path = output_dir / f"{stem}.png"
    html_path = output_dir / f"{stem}.html"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=15_000)
            except Exception:
                pass

        import time
        if delay > 0:
            time.sleep(delay)

        title = page.title()
        page.screenshot(path=str(screenshot_path), full_page=True)
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
        browser.close()

    return BrowseResult(
        url=url,
        output_dir=output_dir,
        screenshot=screenshot_path,
        html_file=html_path,
        title=title,
    )


# ---------------------------------------------------------------------------
# scrape_page
# ---------------------------------------------------------------------------

def scrape_page(
    url: str,
    selector: str,
    output_dir: Path,
    wait_for: str | None = None,
    screenshot: bool = True,
) -> ScrapeResult:
    """Extract elements matching a CSS selector and save as JSON."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=15_000)
            except Exception:
                pass

        items = page.eval_on_selector_all(
            selector,
            """els => els.map(el => ({
                text: el.innerText.trim().slice(0, 500),
                html: el.outerHTML.slice(0, 2000),
                href: el.tagName === 'A' ? el.href : (el.querySelector('a') ? el.querySelector('a').href : null),
                src:  el.tagName === 'IMG' ? el.src : (el.querySelector('img') ? el.querySelector('img').src : null),
            }))""",
        )

        shot_path: Path | None = None
        if screenshot:
            shot_path = output_dir / f"{stem}.png"
            page.screenshot(path=str(shot_path), full_page=True)

        browser.close()

    data_path = output_dir / f"{stem}_scraped.json"
    data_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    return ScrapeResult(
        url=url,
        selector=selector,
        output_dir=output_dir,
        items=items,
        screenshot=shot_path,
        html_file=None,
    )


# ---------------------------------------------------------------------------
# intercept_network
# ---------------------------------------------------------------------------

def intercept_network(
    url: str,
    output_dir: Path,
    block_domains: list[str] | None = None,
    mock_endpoints: dict[str, str] | None = None,
    screenshot: bool = True,
) -> InterceptResult:
    """Load a page, log all network requests, optionally block/mock endpoints."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    block_domains = block_domains or []
    mock_endpoints = mock_endpoints or {}

    captured: list[dict] = []
    blocked_count = 0

    def _on_request(req):
        nonlocal blocked_count
        req_url = req.url
        resource_type = req.resource_type

        for domain in block_domains:
            if domain in req_url:
                blocked_count += 1
                return

        captured.append({
            "url": req_url,
            "method": req.method,
            "resource_type": resource_type,
            "headers": dict(req.headers),
        })

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        if block_domains:
            def _route_handler(route):
                for domain in block_domains:
                    if domain in route.request.url:
                        route.abort()
                        return
                route.continue_()
            ctx.route("**/*", _route_handler)

        for pattern, body in mock_endpoints.items():
            ctx.route(pattern, lambda route, b=body: route.fulfill(body=b, content_type="application/json"))

        page.on("request", _on_request)
        page.goto(url, wait_until="networkidle", timeout=60_000)

        shot_path: Path | None = None
        if screenshot:
            shot_path = output_dir / f"{stem}.png"
            page.screenshot(path=str(shot_path), full_page=True)

        browser.close()

    log_path = output_dir / f"{stem}_network.json"
    log_path.write_text(json.dumps(captured, indent=2, ensure_ascii=False), encoding="utf-8")

    return InterceptResult(
        url=url,
        output_dir=output_dir,
        requests_file=log_path,
        screenshot=shot_path,
        request_count=len(captured),
        blocked_count=blocked_count,
    )


# ---------------------------------------------------------------------------
# run_accessibility_audit
# ---------------------------------------------------------------------------

_AXCORE_JS = """
(function() {
    const violations = [];
    const passes = [];

    function checkElement(el, rule, test, description) {
        if (!test(el)) {
            violations.push({
                rule: rule,
                description: description,
                element: el.outerHTML.slice(0, 200),
                selector: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className ? '.' + el.className.toString().trim().replace(/\\s+/g, '.') : ''),
            });
        } else {
            passes.push(rule);
        }
    }

    // Images must have alt text
    document.querySelectorAll('img').forEach(img => {
        checkElement(img, 'image-alt', el => el.hasAttribute('alt'), 'Images must have alt text');
    });

    // Buttons must have accessible text
    document.querySelectorAll('button').forEach(btn => {
        checkElement(btn, 'button-name', el => (el.innerText || '').trim() || el.getAttribute('aria-label') || el.getAttribute('title'), 'Buttons must have accessible name');
    });

    // Form inputs must have labels
    document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"])').forEach(input => {
        const id = input.id;
        const hasLabel = id && document.querySelector('label[for="' + id + '"]');
        const hasAriaLabel = input.getAttribute('aria-label') || input.getAttribute('aria-labelledby') || input.getAttribute('placeholder');
        checkElement(input, 'label', () => hasLabel || hasAriaLabel, 'Inputs must have associated labels');
    });

    // Links must have accessible text
    document.querySelectorAll('a').forEach(a => {
        checkElement(a, 'link-name', el => (el.innerText || '').trim() || el.getAttribute('aria-label'), 'Links must have accessible text');
    });

    // Page must have a title
    const hasTitle = !!document.title;
    if (hasTitle) passes.push('document-title');
    else violations.push({ rule: 'document-title', description: 'Page must have a title', element: '<title>', selector: 'title' });

    // Heading order (no skipping)
    const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(h => parseInt(h.tagName[1]));
    let headingViolation = false;
    for (let i = 1; i < headings.length; i++) {
        if (headings[i] - headings[i - 1] > 1) { headingViolation = true; break; }
    }
    if (headingViolation) violations.push({ rule: 'heading-order', description: 'Heading levels should not be skipped', element: '', selector: '' });
    else passes.push('heading-order');

    return { violations, passes: passes.length };
})()
"""


def run_accessibility_audit(
    url: str,
    output_dir: Path,
    echo_fn: Callable[[str], None] | None = None,
) -> AccessibilityResult:
    """Run a lightweight accessibility audit against a URL."""
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)
        _echo("  Auditing accessibility...")

        audit = page.evaluate(_AXCORE_JS)
        browser.close()

    violations = audit.get("violations", [])
    passes = audit.get("passes", 0)

    report_lines = [
        f"# Accessibility Audit: {url}",
        "",
        f"Violations: {len(violations)}  |  Passes: {passes}",
        "",
    ]
    if violations:
        report_lines.append("## Violations")
        for v in violations:
            report_lines.append(f"- [{v['rule']}] {v['description']}")
            if v.get("selector"):
                report_lines.append(f"  Element: `{v['selector']}`")
    else:
        report_lines.append("No violations found.")

    report_path = output_dir / f"{stem}_accessibility.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    json_path = output_dir / f"{stem}_accessibility.json"
    json_path.write_text(
        json.dumps({"url": url, "violations": violations, "passes": passes}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return AccessibilityResult(
        url=url,
        output_dir=output_dir,
        report_file=report_path,
        violations=violations,
        passes=passes,
    )


# ---------------------------------------------------------------------------
# run_parallel_tests
# ---------------------------------------------------------------------------

def run_parallel_tests(
    test_file: Path,
    browsers: list[str],
    output_dir: Path,
    echo_fn: Callable[[str], None] | None = None,
) -> ParallelTestResult:
    """Run a Playwright pytest file across multiple browser engines."""
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}

    for browser in browsers:
        _echo(f"  Running on {browser}...")
        report_path = output_dir / f"report_{browser}.html"
        cmd = [
            sys.executable, "-m", "pytest",
            str(test_file),
            f"--browser={browser}",
            f"--html={report_path}",
            "--self-contained-html",
            "-v",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        passed = proc.returncode == 0
        results[browser] = passed
        status = "passed" if passed else "failed"
        _echo(f"    {browser}: {status}")

    return ParallelTestResult(
        test_file=test_file,
        browsers=browsers,
        output_dir=output_dir,
        results=results,
    )


# ---------------------------------------------------------------------------
# capture_trace
# ---------------------------------------------------------------------------

def capture_trace(
    url: str,
    output_dir: Path,
    actions: list[str] | None = None,
    screenshot: bool = True,
    echo_fn: Callable[[str], None] | None = None,
) -> TraceResult:
    """Record a Playwright trace (network + DOM snapshots) while loading a URL."""
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    trace_path = output_dir / f"{stem}_trace.zip"
    shot_path: Path | None = output_dir / f"{stem}.png" if screenshot else None

    _echo(f"  Tracing: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if shot_path:
            page.screenshot(path=str(shot_path), full_page=True)

        ctx.tracing.stop(path=str(trace_path))
        browser.close()

    _echo(f"  Trace saved: {trace_path}")

    return TraceResult(
        url=url,
        output_dir=output_dir,
        trace_file=trace_path,
        screenshot=shot_path,
    )


# ---------------------------------------------------------------------------
# show_trace
# ---------------------------------------------------------------------------

def show_trace(trace_path: Path) -> None:
    """Open a trace ZIP in the Playwright trace viewer."""
    _require_playwright()
    subprocess.run(
        [sys.executable, "-m", "playwright", "show-trace", str(trace_path)],
        check=True,
    )
