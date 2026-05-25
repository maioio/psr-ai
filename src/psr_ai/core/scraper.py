"""Browser scraping, accessibility auditing, network interception, parallel testing, and trace capture.

Covers the full Playwright capability surface:
- All three browser engines: Chromium, Firefox, WebKit
- Headed/headless, slow_mo, custom viewport, devtools, channel (chrome/msedge)
- Mobile device emulation (60+ devices), geolocation, timezone, dark mode, custom user agent
- Network interception, HAR recording/playback, response logging, header modification
- Element screenshots (JPEG/PNG, mask, scale, omit_background, animations)
- PDF generation (margins, headers, footers, page_ranges, landscape)
- Frame/popup/dialog/upload/download/file-chooser handling
- Isolated contexts, incognito, storage state, HTTP auth, cookie management
- JS evaluation, expose_function, expose_binding, add_init_script
- Clock API (set_fixed_time, tick, run_for, pause_at)
- WebSockets monitoring, Service Workers, page error/requestfailed events
- Locator API (get_by_role, get_by_text, get_by_label, get_by_placeholder, etc.)
- Expect assertions (to_be_visible, to_have_text, to_have_url, etc.)
- True parallel execution with ThreadPoolExecutor
- API testing via APIRequestContext
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
    response_count: int = 0

    def report(self) -> str:
        lines = [f"Intercepted: {self.url}"]
        lines.append(f"  Requests logged: {self.request_count}")
        lines.append(f"  Responses logged: {self.response_count}")
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
    failure_screenshots: dict[str, Path] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(self.results.values())

    def report(self) -> str:
        lines = ["Parallel browser test results:"]
        for browser, passed in self.results.items():
            status = "PASS" if passed else "FAIL"
            line = f"  {browser}: {status}"
            if not passed and browser in self.failure_screenshots:
                line += f"  (screenshot: {self.failure_screenshots[browser]})"
            lines.append(line)
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


@dataclass
class VideoResult:
    url: str
    output_dir: Path
    video_file: Path | None = None
    screenshot: Path | None = None

    def report(self) -> str:
        lines = [f"Video recorded: {self.url}"]
        if self.video_file:
            lines.append(f"  Video: {self.video_file}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        return "\n".join(lines)


@dataclass
class ApiTestResult:
    url: str
    method: str
    status_code: int
    body: str
    headers: dict
    ok: bool
    output_file: Path | None = None

    def report(self) -> str:
        lines = [f"API {self.method} {self.url}"]
        lines.append(f"  Status: {self.status_code} ({'OK' if self.ok else 'FAIL'})")
        lines.append(f"  Body length: {len(self.body)} chars")
        if self.output_file:
            lines.append(f"  Saved: {self.output_file}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Browser context builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Device descriptors (mirrors playwright.devices - 60+ devices)
# ---------------------------------------------------------------------------

def _mob(ua: str, w: int, h: int, dpr: float = 2.0) -> dict:
    return {"user_agent": ua, "viewport": {"width": w, "height": h},
            "device_scale_factor": dpr, "is_mobile": True, "has_touch": True}

def _tab(ua: str, w: int, h: int, dpr: float = 2.0) -> dict:
    return {"user_agent": ua, "viewport": {"width": w, "height": h},
            "device_scale_factor": dpr, "is_mobile": True, "has_touch": True}

_IOS15 = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
_IOS16 = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/20A362 Safari/604.1"
_IOS17 = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/21A342 Safari/604.1"
_IPAD15 = "Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
_IPAD16 = "Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/20A362 Safari/604.1"
_CH90  = "Mozilla/5.0 (Linux; Android 11; {model}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Mobile Safari/537.36"
_CH112 = "Mozilla/5.0 (Linux; Android 13; {model}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"

DEVICE_MAP: dict[str, dict] = {
    # ── iPhone ──────────────────────────────────────────────────────────────
    "iphone_6":          _mob(_IOS15, 375, 667, 2),
    "iphone_6_plus":     _mob(_IOS15, 414, 736, 3),
    "iphone_7":          _mob(_IOS15, 375, 667, 2),
    "iphone_7_plus":     _mob(_IOS15, 414, 736, 3),
    "iphone_8":          _mob(_IOS15, 375, 667, 2),
    "iphone_8_plus":     _mob(_IOS15, 414, 736, 3),
    "iphone_se":         _mob(_IOS15, 375, 667, 2),
    "iphone_x":          _mob(_IOS15, 375, 812, 3),
    "iphone_xr":         _mob(_IOS15, 414, 896, 2),
    "iphone_11":         _mob(_IOS15, 414, 896, 2),
    "iphone_11_pro":     _mob(_IOS15, 375, 812, 3),
    "iphone_11_pro_max": _mob(_IOS15, 414, 896, 3),
    "iphone_12":         _mob(_IOS15, 390, 844, 3),
    "iphone_12_mini":    _mob(_IOS15, 375, 812, 3),
    "iphone_12_pro":     _mob(_IOS15, 390, 844, 3),
    "iphone_12_pro_max": _mob(_IOS15, 428, 926, 3),
    "iphone_13":         _mob(_IOS15, 390, 844, 3),
    "iphone_13_mini":    _mob(_IOS15, 375, 812, 3),
    "iphone_13_pro":     _mob(_IOS15, 390, 844, 3),
    "iphone_13_pro_max": _mob(_IOS15, 428, 926, 3),
    "iphone_14":         _mob(_IOS16, 390, 844, 3),
    "iphone_14_plus":    _mob(_IOS16, 428, 926, 3),
    "iphone_14_pro":     _mob(_IOS16, 393, 852, 3),
    "iphone_14_pro_max": _mob(_IOS16, 430, 932, 3),
    "iphone_15":         _mob(_IOS17, 393, 852, 3),
    "iphone_15_pro":     _mob(_IOS17, 393, 852, 3),
    "iphone_15_pro_max": _mob(_IOS17, 430, 932, 3),
    # ── iPad ────────────────────────────────────────────────────────────────
    "ipad":              _tab(_IPAD15, 768, 1024, 2),
    "ipad_mini":         _tab(_IPAD15, 768, 1024, 2),
    "ipad_air":          _tab(_IPAD16, 820, 1180, 2),
    "ipad_pro_11":       _tab(_IPAD16, 834, 1194, 2),
    "ipad_pro_12":       _tab(_IPAD16, 1024, 1366, 2),
    "ipad_pro":          _tab(_IPAD15, 1024, 1366, 2),  # backward compat alias
    # ── Google Pixel ────────────────────────────────────────────────────────
    "pixel_2":           _mob(_CH90.format(model="Pixel 2"),         411, 731, 2.625),
    "pixel_2_xl":        _mob(_CH90.format(model="Pixel 2 XL"),      411, 823, 3.5),
    "pixel_3":           _mob(_CH90.format(model="Pixel 3"),         393, 786, 2.75),
    "pixel_4":           _mob(_CH90.format(model="Pixel 4"),         353, 745, 3),
    "pixel_4a":          _mob(_CH90.format(model="Pixel 4a"),        393, 851, 2.75),
    "pixel_5":           _mob(_CH90.format(model="Pixel 5"),         393, 851, 2.75),
    "pixel_7":           _mob(_CH112.format(model="Pixel 7"),        412, 915, 2.625),
    # ── Samsung Galaxy ──────────────────────────────────────────────────────
    "galaxy_s5":         _mob("Mozilla/5.0 (Linux; Android 5.0; SM-G900P) AppleWebKit/537.36 Chrome/75.0.3770.100 Mobile Safari/537.36", 360, 640, 3),
    "galaxy_s8":         _mob(_CH90.format(model="Samsung Galaxy S8"), 360, 740, 3),
    "galaxy_s9_plus":    _mob(_CH90.format(model="Samsung Galaxy S9+"), 320, 658, 4.5),
    "galaxy_s21":        _mob(_CH90.format(model="Samsung Galaxy S21"), 360, 800, 3),
    "galaxy_s23":        _mob(_CH112.format(model="Samsung Galaxy S23"), 360, 780, 3),
    "galaxy_tab_s4":     _tab(_CH90.format(model="Samsung Galaxy Tab S4"), 712, 1138, 2.25),
    # ── Other Android ───────────────────────────────────────────────────────
    "nexus_5":           _mob("Mozilla/5.0 (Linux; Android 6.0; Nexus 5) AppleWebKit/537.36 Chrome/90.0 Mobile Safari/537.36", 360, 640, 3),
    "nexus_5x":          _mob("Mozilla/5.0 (Linux; Android 8.0; Nexus 5X) AppleWebKit/537.36 Chrome/90.0 Mobile Safari/537.36", 412, 732, 2.625),
    "nexus_6":           _mob("Mozilla/5.0 (Linux; Android 7.1; Nexus 6) AppleWebKit/537.36 Chrome/90.0 Mobile Safari/537.36", 412, 732, 3.5),
    "nexus_6p":          _mob("Mozilla/5.0 (Linux; Android 8.0; Nexus 6P) AppleWebKit/537.36 Chrome/90.0 Mobile Safari/537.36", 412, 732, 3.5),
    "nexus_7":           _tab("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 7) AppleWebKit/537.36 Chrome/90.0 Safari/537.36", 600, 960, 2),
    "nexus_10":          _tab("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 10) AppleWebKit/537.36 Chrome/90.0 Safari/537.36", 800, 1280, 2),
    "moto_g4":           _mob("Mozilla/5.0 (Linux; Android 7.0; Moto G (4)) AppleWebKit/537.36 Chrome/90.0 Mobile Safari/537.36", 360, 640, 3),
    "oneplus_6t":        _mob(_CH90.format(model="OnePlus 6T"), 412, 892, 2.625),
    # ── Desktop ─────────────────────────────────────────────────────────────
    "desktop_1080p":     {"viewport": {"width": 1920, "height": 1080}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
    "desktop_1440p":     {"viewport": {"width": 2560, "height": 1440}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
    "desktop_4k":        {"viewport": {"width": 3840, "height": 2160}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
    "laptop_hdpi":       {"viewport": {"width": 1280, "height": 800},  "device_scale_factor": 2, "is_mobile": False, "has_touch": False},
}


def _build_context_options(
    viewport: dict | None = None,
    device: str | None = None,
    geolocation: dict | None = None,
    timezone: str | None = None,
    dark_mode: bool = False,
    user_agent: str | None = None,
    http_credentials: dict | None = None,
    storage_state: str | Path | None = None,
    extra_http_headers: dict | None = None,
    record_video_dir: Path | None = None,
    record_har: Path | None = None,
    locale: str | None = None,
    permissions: list[str] | None = None,
    bypass_csp: bool = False,
    ignore_https_errors: bool = False,
    java_script_enabled: bool = True,
    color_scheme: str | None = None,
    proxy: dict | None = None,
    accept_downloads: bool = True,
) -> dict:
    """Build a new_context() kwargs dict from high-level options."""
    opts: dict = {}

    if device and device.lower() in DEVICE_MAP:
        dev = DEVICE_MAP[device.lower()]
        opts.update(dev)
    else:
        opts["viewport"] = viewport or {"width": 1280, "height": 900}

    if geolocation:
        opts["geolocation"] = geolocation
        perms = list(permissions or [])
        if "geolocation" not in perms:
            perms.append("geolocation")
        opts["permissions"] = perms
    elif permissions:
        opts["permissions"] = permissions

    if timezone:
        opts["timezone_id"] = timezone

    if locale:
        opts["locale"] = locale

    if dark_mode:
        opts["color_scheme"] = "dark"
    elif color_scheme:
        opts["color_scheme"] = color_scheme

    if user_agent:
        opts["user_agent"] = user_agent

    if http_credentials:
        opts["http_credentials"] = http_credentials

    if storage_state:
        opts["storage_state"] = str(storage_state)

    if extra_http_headers:
        opts["extra_http_headers"] = extra_http_headers

    if record_video_dir:
        record_video_dir = Path(record_video_dir)
        record_video_dir.mkdir(parents=True, exist_ok=True)
        opts["record_video_dir"] = str(record_video_dir)

    if record_har:
        opts["record_har_path"] = str(record_har)

    if bypass_csp:
        opts["bypass_csp"] = True

    if ignore_https_errors:
        opts["ignore_https_errors"] = True

    if not java_script_enabled:
        opts["java_script_enabled"] = False

    if proxy:
        opts["proxy"] = proxy

    opts["accept_downloads"] = accept_downloads

    return opts


def _get_browser(
    p,
    browser_name: str,
    headless: bool = True,
    slow_mo: float = 0.0,
    devtools: bool = False,
    channel: str | None = None,
):
    """Launch the named browser engine.

    channel: "chrome", "chrome-beta", "msedge", etc. (Chromium only).
    devtools: open DevTools automatically (Chromium only, forces headed).
    """
    kwargs: dict = {"headless": headless}
    if slow_mo > 0:
        kwargs["slow_mo"] = slow_mo
    name = browser_name.lower()
    if name == "firefox":
        return p.firefox.launch(**kwargs)
    if name in ("webkit", "safari"):
        return p.webkit.launch(**kwargs)
    # Chromium extras
    if channel:
        kwargs["channel"] = channel
    if devtools:
        kwargs["devtools"] = True
        kwargs["headless"] = False
    return p.chromium.launch(**kwargs)


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
    browser_name: str = "chromium",
    headless: bool = True,
    viewport: dict | None = None,
    device: str | None = None,
    user_agent: str | None = None,
    dark_mode: bool = False,
    slow_mo: float = 0.0,
) -> BrowseResult:
    """Full-page screenshot + raw HTML capture of a URL."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    screenshot_path = output_dir / f"{stem}.png"
    html_path = output_dir / f"{stem}.html"

    ctx_opts = _build_context_options(
        viewport=viewport,
        device=device,
        dark_mode=dark_mode,
        user_agent=user_agent,
    )

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless, slow_mo=slow_mo)
        ctx = browser.new_context(**ctx_opts)
        page = ctx.new_page()
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
    browser_name: str = "chromium",
    headless: bool = True,
) -> ScrapeResult:
    """Extract elements matching a CSS selector and save as JSON."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
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
# screenshot_element  (element screenshot + clip + buffer)
# ---------------------------------------------------------------------------

def screenshot_element(
    url: str,
    selector: str,
    output_dir: Path,
    clip: dict | None = None,
    to_buffer: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> dict:
    """Capture a screenshot of a specific element or clipped region.

    Returns dict with keys: 'file' (Path or None), 'buffer' (bytes or None).
    clip dict: {"x": int, "y": int, "width": int, "height": int}
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    result: dict = {"file": None, "buffer": None}

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if clip:
            # Region clip
            img_bytes = page.screenshot(clip=clip)
            if to_buffer:
                result["buffer"] = img_bytes
            else:
                out_path = output_dir / f"{stem}_clip.png"
                out_path.write_bytes(img_bytes)
                result["file"] = out_path
        else:
            # Element screenshot
            try:
                locator = page.locator(selector).first
                if to_buffer:
                    result["buffer"] = locator.screenshot()
                else:
                    out_path = output_dir / f"{stem}_element.png"
                    locator.screenshot(path=str(out_path))
                    result["file"] = out_path
            except Exception:
                # Fallback to full-page screenshot
                img_bytes = page.screenshot(full_page=True)
                if to_buffer:
                    result["buffer"] = img_bytes
                else:
                    out_path = output_dir / f"{stem}_fullpage.png"
                    out_path.write_bytes(img_bytes)
                    result["file"] = out_path

        browser.close()

    return result


# ---------------------------------------------------------------------------
# record_video
# ---------------------------------------------------------------------------

def record_video(
    url: str,
    output_dir: Path,
    actions: list[dict] | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
    viewport: dict | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> VideoResult:
    """Record a video of browser interaction at a URL.

    Uses Playwright's record_video_dir context option. The video is saved
    as a .webm file in output_dir after the context closes.
    """
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    video_dir = output_dir / "video"
    video_dir.mkdir(exist_ok=True)
    stem = _safe_filename(url)

    ctx_opts = _build_context_options(
        viewport=viewport,
        record_video_dir=video_dir,
    )

    video_path: Path | None = None
    shot_path: Path | None = None

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(**ctx_opts)
        page = ctx.new_page()

        _echo(f"  Recording video: {url}")
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if actions:
            import time as _time
            for act in actions:
                atype = act.get("type", "")
                if atype == "click":
                    page.click(act["selector"])
                elif atype == "fill":
                    page.fill(act["selector"], act.get("value", ""))
                elif atype == "wait":
                    _time.sleep(act.get("ms", 500) / 1000)

        shot_path = output_dir / f"{stem}.png"
        page.screenshot(path=str(shot_path), full_page=True)

        # Closing the context finalizes the video file.
        ctx.close()

        # Find the video file (Playwright names it with a UUID).
        video_files = sorted(video_dir.glob("*.webm"))
        if video_files:
            # Rename to a predictable name.
            target = output_dir / f"{stem}.webm"
            video_files[-1].rename(target)
            video_path = target

        browser.close()

    _echo(f"  Video saved: {video_path}")

    return VideoResult(
        url=url,
        output_dir=output_dir,
        video_file=video_path,
        screenshot=shot_path,
    )


# ---------------------------------------------------------------------------
# intercept_network  (full request + response logging)
# ---------------------------------------------------------------------------

def intercept_network(
    url: str,
    output_dir: Path,
    block_domains: list[str] | None = None,
    mock_endpoints: dict[str, str] | None = None,
    modify_headers: dict[str, str] | None = None,
    wait_for_url: str | None = None,
    screenshot: bool = True,
    browser_name: str = "chromium",
    headless: bool = True,
) -> InterceptResult:
    """Load a page, log all network requests AND responses, optionally block/mock/modify."""
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    block_domains = block_domains or []
    mock_endpoints = mock_endpoints or {}
    modify_headers = modify_headers or {}

    captured_requests: list[dict] = []
    captured_responses: list[dict] = []
    blocked_count = 0
    lock = threading.Lock()

    def _on_request(req):
        nonlocal blocked_count
        req_url = req.url
        for domain in block_domains:
            if domain in req_url:
                with lock:
                    blocked_count += 1
                return
        with lock:
            captured_requests.append({
                "url": req_url,
                "method": req.method,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
            })

    def _on_response(resp):
        try:
            with lock:
                captured_responses.append({
                    "url": resp.url,
                    "status": resp.status,
                    "ok": resp.ok,
                    "headers": dict(resp.headers),
                })
        except Exception:
            pass

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx_opts: dict = {"viewport": {"width": 1280, "height": 900}}
        if modify_headers:
            ctx_opts["extra_http_headers"] = modify_headers
        ctx = browser.new_context(**ctx_opts)
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
            ctx.route(
                pattern,
                lambda route, b=body: route.fulfill(
                    body=b, content_type="application/json"
                ),
            )

        page.on("request", _on_request)
        page.on("response", _on_response)

        if wait_for_url:
            with page.expect_request(wait_for_url, timeout=30_000):
                page.goto(url, wait_until="networkidle", timeout=60_000)
        else:
            page.goto(url, wait_until="networkidle", timeout=60_000)

        shot_path: Path | None = None
        if screenshot:
            shot_path = output_dir / f"{stem}.png"
            page.screenshot(path=str(shot_path), full_page=True)

        browser.close()

    log = {
        "url": url,
        "requests": captured_requests,
        "responses": captured_responses,
        "blocked": blocked_count,
    }
    log_path = output_dir / f"{stem}_network.json"
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

    return InterceptResult(
        url=url,
        output_dir=output_dir,
        requests_file=log_path,
        screenshot=shot_path,
        request_count=len(captured_requests),
        blocked_count=blocked_count,
        response_count=len(captured_responses),
    )


# ---------------------------------------------------------------------------
# handle_page_interactions  (frames, popups, dialogs, upload, download)
# ---------------------------------------------------------------------------

def handle_page_interactions(
    url: str,
    output_dir: Path,
    upload_selector: str | None = None,
    upload_file: Path | None = None,
    dialog_response: str | None = None,
    wait_for_download: bool = False,
    wait_for_popup: bool = False,
    frame_selector: str | None = None,
    frame_action: dict | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
    echo_fn: Callable[[str], None] | None = None,
) -> dict:
    """Demonstrate frame/popup/dialog/upload/download interaction patterns.

    Returns a dict with keys: 'screenshot', 'download_path', 'popup_title',
    'dialog_message', 'frame_content'.
    """
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    result: dict = {
        "screenshot": None,
        "download_path": None,
        "popup_title": None,
        "dialog_message": None,
        "frame_content": None,
    }

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = ctx.new_page()

        # Dialog handling
        dialog_messages: list[str] = []
        def _on_dialog(dialog):
            dialog_messages.append(dialog.message)
            _echo(f"  Dialog: {dialog.type} - {dialog.message!r}")
            if dialog_response is not None:
                if dialog.type == "prompt":
                    dialog.accept(dialog_response)
                else:
                    dialog.accept()
            else:
                dialog.dismiss()
        page.on("dialog", _on_dialog)

        page.goto(url, wait_until="networkidle", timeout=60_000)

        # File upload
        if upload_selector and upload_file:
            _echo(f"  Uploading: {upload_file}")
            page.set_input_files(upload_selector, str(upload_file))

        # Frame interaction
        if frame_selector:
            try:
                frame = page.frame_locator(frame_selector)
                if frame_action:
                    atype = frame_action.get("type", "")
                    if atype == "click":
                        frame.locator(frame_action.get("selector", "body")).click()
                    elif atype == "fill":
                        frame.locator(frame_action.get("selector", "body")).fill(
                            frame_action.get("value", "")
                        )
                    result["frame_content"] = "interacted"
            except Exception as exc:
                _echo(f"  Frame error: {exc}")

        # Popup handling
        if wait_for_popup:
            try:
                with page.expect_popup(timeout=10_000) as popup_info:
                    page.keyboard.press("Tab")  # trigger any pending popup
                popup_page = popup_info.value
                result["popup_title"] = popup_page.title()
                popup_page.close()
                _echo(f"  Popup title: {result['popup_title']}")
            except Exception:
                pass

        # Download handling
        if wait_for_download:
            try:
                with page.expect_download(timeout=10_000) as dl_info:
                    page.keyboard.press("Tab")
                download = dl_info.value
                dl_path = output_dir / download.suggested_filename
                download.save_as(str(dl_path))
                result["download_path"] = dl_path
                _echo(f"  Downloaded: {dl_path.name}")
            except Exception:
                pass

        if dialog_messages:
            result["dialog_message"] = dialog_messages[0]

        shot_path = output_dir / f"{stem}_interactions.png"
        page.screenshot(path=str(shot_path), full_page=True)
        result["screenshot"] = shot_path

        browser.close()

    return result


# ---------------------------------------------------------------------------
# browser_context_advanced  (incognito, storage_state, HTTP auth, JS init)
# ---------------------------------------------------------------------------

def browse_with_context(
    url: str,
    output_dir: Path,
    incognito: bool = False,
    storage_state_in: str | Path | None = None,
    storage_state_out: str | Path | None = None,
    http_credentials: dict | None = None,
    init_script: str | None = None,
    expose_fn_name: str | None = None,
    expose_fn: Callable | None = None,
    geolocation: dict | None = None,
    timezone: str | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
    echo_fn: Callable[[str], None] | None = None,
) -> BrowseResult:
    """Browse with advanced context options: incognito, storage state, HTTP auth, JS injection."""
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    screenshot_path = output_dir / f"{stem}_ctx.png"
    html_path = output_dir / f"{stem}_ctx.html"

    ctx_opts = _build_context_options(
        http_credentials=http_credentials,
        storage_state=storage_state_in if not incognito else None,
        geolocation=geolocation,
        timezone=timezone,
    )

    if incognito:
        # Incognito = isolated context with no shared storage
        ctx_opts.pop("storage_state", None)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(**ctx_opts)

        if init_script:
            ctx.add_init_script(init_script)
            _echo("  Injected init script")

        if expose_fn_name and expose_fn:
            page = ctx.new_page()
            page.expose_function(expose_fn_name, expose_fn)
            _echo(f"  Exposed function: {expose_fn_name}")
        else:
            page = ctx.new_page()

        page.goto(url, wait_until="networkidle", timeout=60_000)
        title = page.title()
        page.screenshot(path=str(screenshot_path), full_page=True)
        html = page.content()
        html_path.write_text(html, encoding="utf-8")

        if storage_state_out:
            ctx.storage_state(path=str(storage_state_out))
            _echo(f"  Saved storage state: {storage_state_out}")

        browser.close()

    return BrowseResult(
        url=url,
        output_dir=output_dir,
        screenshot=screenshot_path,
        html_file=html_path,
        title=title,
    )


# ---------------------------------------------------------------------------
# js_advanced  (evaluate_handle, expose_function, add_init_script)
# ---------------------------------------------------------------------------

def js_evaluate(
    url: str,
    script: str,
    output_dir: Path,
    use_handle: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> dict:
    """Execute JavaScript on a page and return the result.

    use_handle=True uses evaluate_handle (returns a JSHandle for complex objects).
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if use_handle:
            handle = page.evaluate_handle(script)
            # Serialize handle to JSON-safe value
            try:
                value = handle.json_value()
            except Exception:
                value = str(handle)
            finally:
                handle.dispose()
        else:
            value = page.evaluate(script)

        browser.close()

    result_path = output_dir / f"{_safe_filename(url)}_js_result.json"
    result_path.write_text(
        json.dumps({"url": url, "script": script, "result": value}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"url": url, "result": value, "file": result_path}


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

    // Keyboard navigation: interactive elements must be focusable
    document.querySelectorAll('button, a, input, select, textarea, [role="button"], [role="link"]').forEach(el => {
        const tabindex = el.getAttribute('tabindex');
        checkElement(el, 'keyboard-access', () => tabindex !== '-1', 'Interactive elements must be keyboard accessible (tabindex != -1)');
    });

    // ARIA roles validation (basic)
    document.querySelectorAll('[role]').forEach(el => {
        const role = el.getAttribute('role');
        const validRoles = ['button', 'link', 'navigation', 'main', 'complementary', 'banner', 'contentinfo', 'search', 'form', 'dialog', 'alert', 'alertdialog', 'tab', 'tablist', 'tabpanel', 'menu', 'menuitem', 'menubar', 'listbox', 'option', 'combobox', 'grid', 'gridcell', 'row', 'columnheader', 'rowheader', 'img', 'presentation', 'none', 'region', 'article', 'heading', 'list', 'listitem', 'checkbox', 'radio', 'textbox', 'spinbutton', 'slider', 'progressbar', 'status', 'log', 'marquee', 'timer', 'tooltip', 'tree', 'treeitem', 'treegrid'];
        checkElement(el, 'aria-role-valid', () => validRoles.includes(role), 'Element has invalid ARIA role: ' + role);
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

    // Accessibility tree snapshot (subset)
    const tree = Array.from(document.querySelectorAll('[role], button, a, input, select, textarea, h1, h2, h3, h4, h5, h6')).slice(0, 50).map(el => ({
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') || el.tagName.toLowerCase(),
        name: el.getAttribute('aria-label') || el.innerText?.trim().slice(0, 60) || el.getAttribute('placeholder') || '',
        level: el.tagName.match(/^H(\\d)$/) ? parseInt(el.tagName[1]) : null,
    }));

    return { violations, passes: passes.length, tree };
})()
"""


def run_accessibility_audit(
    url: str,
    output_dir: Path,
    echo_fn: Callable[[str], None] | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
) -> AccessibilityResult:
    """Run a comprehensive accessibility audit against a URL.

    Checks: image alt text, button names, input labels, link text, keyboard
    accessibility, ARIA role validity, document title, heading order.
    Also captures the accessibility tree snapshot.
    """
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)
        _echo("  Auditing accessibility...")

        audit = page.evaluate(_AXCORE_JS)

        # Native accessibility snapshot (removed in Playwright >= 1.44; best-effort)
        try:
            ax_snapshot = page.accessibility.snapshot() or {}  # type: ignore[attr-defined]
        except AttributeError:
            ax_snapshot = {}
        browser.close()

    violations = audit.get("violations", [])
    passes = audit.get("passes", 0)
    tree = audit.get("tree", [])

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

    if tree:
        report_lines.append("\n## Accessibility Tree (top 50 elements)")
        for node in tree:
            indent = "  " * (node.get("level", 1) or 1)
            role = node.get("role", "")
            name = node.get("name", "")
            report_lines.append(f"{indent}[{role}] {name}")

    report_path = output_dir / f"{stem}_accessibility.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    json_path = output_dir / f"{stem}_accessibility.json"
    json_path.write_text(
        json.dumps(
            {
                "url": url,
                "violations": violations,
                "passes": passes,
                "accessibility_tree": ax_snapshot,
                "element_tree": tree,
            },
            indent=2,
            ensure_ascii=False,
        ),
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
# run_parallel_tests  (true parallel via ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _run_browser_test(
    test_file: Path,
    browser: str,
    output_dir: Path,
) -> tuple[str, bool, Path | None]:
    """Run pytest for one browser in a worker thread. Returns (browser, passed, screenshot_path)."""
    report_path = output_dir / f"report_{browser}.html"
    screenshot_path = output_dir / f"failure_{browser}.png"

    cmd = [
        sys.executable, "-m", "pytest",
        str(test_file),
        f"--browser={browser}",
        f"--html={report_path}",
        "--self-contained-html",
        "--screenshot=only-on-failure",
        f"--output={output_dir}",
        "-v",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    passed = proc.returncode == 0

    # Check for failure screenshot produced by pytest-playwright
    actual_shot: Path | None = None
    if not passed and screenshot_path.exists():
        actual_shot = screenshot_path

    return browser, passed, actual_shot


def run_parallel_tests(
    test_file: Path,
    browsers: list[str],
    output_dir: Path,
    echo_fn: Callable[[str], None] | None = None,
) -> ParallelTestResult:
    """Run a Playwright pytest file across multiple browser engines IN PARALLEL."""
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}
    failure_screenshots: dict[str, Path] = {}

    _echo(f"  Launching {len(browsers)} browser(s) in parallel: {', '.join(browsers)}")

    with ThreadPoolExecutor(max_workers=len(browsers)) as executor:
        futures = {
            executor.submit(_run_browser_test, test_file, b, output_dir): b
            for b in browsers
        }
        for future in as_completed(futures):
            browser_name_fut = futures[future]
            try:
                b, passed, shot = future.result()
                results[b] = passed
                if shot:
                    failure_screenshots[b] = shot
                status = "PASS" if passed else "FAIL"
                _echo(f"  {b}: {status}")
            except Exception as exc:
                results[browser_name_fut] = False
                _echo(f"  {browser_name_fut}: ERROR ({exc})")

    return ParallelTestResult(
        test_file=test_file,
        browsers=browsers,
        output_dir=output_dir,
        results=results,
        failure_screenshots=failure_screenshots,
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
    browser_name: str = "chromium",
    headless: bool = True,
) -> TraceResult:
    """Record a Playwright trace (network + DOM snapshots + console) while loading a URL."""
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
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = ctx.new_page()
        # Capture console logs during trace
        console_msgs: list[str] = []
        page.on("console", lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if shot_path:
            page.screenshot(path=str(shot_path), full_page=True)

        ctx.tracing.stop(path=str(trace_path))
        browser.close()

    # Save console log alongside trace
    if console_msgs:
        console_path = output_dir / f"{stem}_console.txt"
        console_path.write_text("\n".join(console_msgs), encoding="utf-8")

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


# ---------------------------------------------------------------------------
# api_request  (Playwright APIRequestContext - no browser needed)
# ---------------------------------------------------------------------------

def api_request(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    headers: dict | None = None,
    output_dir: Path | None = None,
    storage_state: str | Path | None = None,
    assert_status: int | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> ApiTestResult:
    """Send an HTTP request using Playwright's APIRequestContext (no browser window).

    Supports reusing browser auth session via storage_state.
    """
    _require_playwright()

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    _echo(f"  API {method.upper()} {url}")

    with sync_playwright() as p:
        req_opts: dict = {}
        if storage_state:
            req_opts["storage_state"] = str(storage_state)
        if headers:
            req_opts["extra_http_headers"] = headers

        request_ctx = p.request.new_context(**req_opts)

        method_upper = method.upper()
        if method_upper == "GET":
            resp = request_ctx.get(url)
        elif method_upper == "POST":
            resp = request_ctx.post(url, data=json.dumps(data) if data else None)
        elif method_upper == "PUT":
            resp = request_ctx.put(url, data=json.dumps(data) if data else None)
        elif method_upper == "DELETE":
            resp = request_ctx.delete(url)
        elif method_upper == "PATCH":
            resp = request_ctx.patch(url, data=json.dumps(data) if data else None)
        else:
            request_ctx.dispose()
            raise ValueError(f"Unsupported HTTP method: {method}")

        status_code = resp.status
        ok = resp.ok
        resp_headers = dict(resp.headers)
        try:
            body = resp.text()
        except Exception:
            body = ""

        request_ctx.dispose()

    if assert_status is not None and status_code != assert_status:
        raise AssertionError(
            f"API {method_upper} {url}: expected status {assert_status}, got {status_code}"
        )

    out_file: Path | None = None
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_filename(url)
        out_file = output_dir / f"{stem}_api_{method_upper.lower()}.json"
        out_file.write_text(
            json.dumps(
                {
                    "url": url,
                    "method": method_upper,
                    "status": status_code,
                    "ok": ok,
                    "headers": resp_headers,
                    "body": body[:10_000],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    result = ApiTestResult(
        url=url,
        method=method_upper,
        status_code=status_code,
        body=body,
        headers=resp_headers,
        ok=ok,
        output_file=out_file,
    )
    _echo(result.report())
    return result


# ---------------------------------------------------------------------------
# 1. Locator API wrapper  (get_by_* + chaining helpers)
# ---------------------------------------------------------------------------

@dataclass
class LocatorResult:
    url: str
    locator_strategy: str
    count: int
    texts: list[str]
    attributes: list[dict]
    screenshot: Path | None = None
    output_dir: Path | None = None

    def report(self) -> str:
        lines = [f"Locator: {self.locator_strategy} on {self.url}"]
        lines.append(f"  Matched: {self.count} element(s)")
        for i, txt in enumerate(self.texts[:5]):
            lines.append(f"  [{i}] {txt!r}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        return "\n".join(lines)


def locate_elements(
    url: str,
    output_dir: Path,
    strategy: str = "css",
    value: str = "body",
    role: str | None = None,
    name: str | None = None,
    label: str | None = None,
    placeholder: str | None = None,
    alt_text: str | None = None,
    title: str | None = None,
    test_id: str | None = None,
    text: str | None = None,
    nth: int | None = None,
    screenshot: bool = True,
    browser_name: str = "chromium",
    headless: bool = True,
) -> LocatorResult:
    """Locate elements using Playwright's modern Locator API.

    Strategies: css, xpath, role, text, label, placeholder, alt_text, title, test_id.
    Returns count, inner texts, and optional screenshot of the first matched element.
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        # Build locator
        if role:
            loc = page.get_by_role(role, name=name) if name else page.get_by_role(role)
            strategy_label = f"get_by_role({role!r})"
        elif label:
            loc = page.get_by_label(label)
            strategy_label = f"get_by_label({label!r})"
        elif placeholder:
            loc = page.get_by_placeholder(placeholder)
            strategy_label = f"get_by_placeholder({placeholder!r})"
        elif alt_text:
            loc = page.get_by_alt_text(alt_text)
            strategy_label = f"get_by_alt_text({alt_text!r})"
        elif title:
            loc = page.get_by_title(title)
            strategy_label = f"get_by_title({title!r})"
        elif test_id:
            loc = page.get_by_test_id(test_id)
            strategy_label = f"get_by_test_id({test_id!r})"
        elif text:
            loc = page.get_by_text(text)
            strategy_label = f"get_by_text({text!r})"
        elif strategy == "xpath":
            loc = page.locator(f"xpath={value}")
            strategy_label = f"xpath={value}"
        else:
            loc = page.locator(value)
            strategy_label = f"css={value}"

        if nth is not None:
            loc = loc.nth(nth)

        count = loc.count()
        texts: list[str] = []
        attrs: list[dict] = []
        for i in range(min(count, 20)):
            el = loc.nth(i)
            try:
                t = el.inner_text()
            except Exception:
                t = ""
            texts.append(t[:200])
            try:
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                href = el.get_attribute("href") or ""
                src = el.get_attribute("src") or ""
                attrs.append({"tag": tag, "href": href, "src": src})
            except Exception:
                attrs.append({})

        shot_path: Path | None = None
        if screenshot and count > 0:
            shot_path = output_dir / f"{stem}_locator.png"
            try:
                loc.first.screenshot(path=str(shot_path))
            except Exception:
                page.screenshot(path=str(shot_path), full_page=True)

        browser.close()

    # Save JSON
    out_json = output_dir / f"{stem}_locator.json"
    out_json.write_text(
        json.dumps({
            "url": url,
            "strategy": strategy_label,
            "count": count,
            "texts": texts,
            "attributes": attrs,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return LocatorResult(
        url=url,
        locator_strategy=strategy_label,
        count=count,
        texts=texts,
        attributes=attrs,
        screenshot=shot_path,
        output_dir=output_dir,
    )


# ---------------------------------------------------------------------------
# 2. Expect assertions  (run assertions against a live page, save report)
# ---------------------------------------------------------------------------

@dataclass
class AssertResult:
    url: str
    assertions: list[dict]
    passed: int
    failed: int
    output_file: Path | None = None

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def report(self) -> str:
        lines = [f"Assertions on {self.url}: {self.passed} passed, {self.failed} failed"]
        for a in self.assertions:
            status = "PASS" if a["passed"] else "FAIL"
            lines.append(f"  [{status}] {a['assertion']}")
            if not a["passed"] and a.get("error"):
                lines.append(f"         {a['error']}")
        return "\n".join(lines)


def run_assertions(
    url: str,
    output_dir: Path,
    assertions: list[dict],
    browser_name: str = "chromium",
    headless: bool = True,
) -> AssertResult:
    """Run Playwright expect() assertions against a live URL.

    Each assertion dict supports:
      {"type": "page_url",   "pattern": "example"}
      {"type": "page_title", "pattern": "Example"}
      {"type": "visible",    "selector": "h1"}
      {"type": "hidden",     "selector": ".spinner"}
      {"type": "enabled",    "selector": "button#submit"}
      {"type": "disabled",   "selector": "button#submit"}
      {"type": "checked",    "selector": "input[type=checkbox]"}
      {"type": "text",       "selector": "h1",     "value": "Example Domain"}
      {"type": "value",      "selector": "input",  "value": "hello"}
      {"type": "count",      "selector": "li",     "value": 3}
      {"type": "attribute",  "selector": "a",      "attr": "href", "value": "/"}
      {"type": "css",        "selector": "body",   "prop": "color", "value": "rgb(0,0,0)"}
      {"type": "focused",    "selector": "input"}
      {"type": "empty",      "selector": ".container"}
      {"type": "screenshot_match"}   (saves baseline or compares)
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import expect as pw_expect

    results: list[dict] = []

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        for a in assertions:
            atype = a.get("type", "")
            passed = False
            error = ""
            label = f"{atype}: {a.get('selector', a.get('pattern', ''))}"

            try:
                if atype == "page_url":
                    pw_expect(page).to_have_url(re.compile(a["pattern"], re.IGNORECASE))
                    passed = True

                elif atype == "page_title":
                    pw_expect(page).to_have_title(re.compile(a["pattern"], re.IGNORECASE))
                    passed = True

                elif atype == "visible":
                    pw_expect(page.locator(a["selector"])).to_be_visible()
                    passed = True

                elif atype == "hidden":
                    pw_expect(page.locator(a["selector"])).to_be_hidden()
                    passed = True

                elif atype == "enabled":
                    pw_expect(page.locator(a["selector"])).to_be_enabled()
                    passed = True

                elif atype == "disabled":
                    pw_expect(page.locator(a["selector"])).to_be_disabled()
                    passed = True

                elif atype == "checked":
                    pw_expect(page.locator(a["selector"])).to_be_checked()
                    passed = True

                elif atype == "focused":
                    pw_expect(page.locator(a["selector"])).to_be_focused()
                    passed = True

                elif atype == "empty":
                    pw_expect(page.locator(a["selector"])).to_be_empty()
                    passed = True

                elif atype == "text":
                    pw_expect(page.locator(a["selector"])).to_have_text(
                        re.compile(str(a["value"]), re.IGNORECASE)
                    )
                    passed = True

                elif atype == "contain_text":
                    pw_expect(page.locator(a["selector"])).to_contain_text(str(a["value"]))
                    passed = True

                elif atype == "value":
                    pw_expect(page.locator(a["selector"])).to_have_value(str(a["value"]))
                    passed = True

                elif atype == "count":
                    pw_expect(page.locator(a["selector"])).to_have_count(int(a["value"]))
                    passed = True

                elif atype == "attribute":
                    pw_expect(page.locator(a["selector"])).to_have_attribute(
                        a["attr"], re.compile(str(a["value"]), re.IGNORECASE)
                    )
                    passed = True

                elif atype == "css":
                    pw_expect(page.locator(a["selector"])).to_have_css(a["prop"], a["value"])
                    passed = True

                elif atype == "js_property":
                    pw_expect(page.locator(a["selector"])).to_have_js_property(
                        a["prop"], a["value"]
                    )
                    passed = True

                elif atype == "screenshot_match":
                    stem = _safe_filename(url)
                    snap_path = output_dir / f"{stem}_snapshot.png"
                    pw_expect(page).to_have_screenshot(snap_path)
                    passed = True

                else:
                    error = f"Unknown assertion type: {atype!r}"

            except Exception as exc:
                error = str(exc)[:300]

            results.append({"assertion": label, "passed": passed, "error": error})

        browser.close()

    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    out_file = output_dir / f"{_safe_filename(url)}_assertions.json"
    out_file.write_text(
        json.dumps({"url": url, "results": results, "passed": passed_count, "failed": failed_count},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return AssertResult(
        url=url,
        assertions=results,
        passed=passed_count,
        failed=failed_count,
        output_file=out_file,
    )


# ---------------------------------------------------------------------------
# 3. HAR recording + playback
# ---------------------------------------------------------------------------

@dataclass
class HarResult:
    url: str
    har_file: Path
    entry_count: int = 0

    def report(self) -> str:
        return (
            f"HAR recorded: {self.url}\n"
            f"  File: {self.har_file}\n"
            f"  Entries: {self.entry_count}"
        )


def record_har(
    url: str,
    output_dir: Path,
    url_filter: str | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
) -> HarResult:
    """Record all network traffic as a HAR file while loading a URL.

    url_filter: glob pattern to include only matching requests (e.g. "**/*.json").
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    har_path = output_dir / f"{stem}.har"

    har_opts: dict = {"path": str(har_path)}
    if url_filter:
        har_opts["url_filter"] = url_filter

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            record_har_path=str(har_path),
            **({"record_har_url_filter": url_filter} if url_filter else {}),
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)
        ctx.close()
        browser.close()

    entry_count = 0
    if har_path.exists():
        try:
            har_data = json.loads(har_path.read_text(encoding="utf-8"))
            entry_count = len(har_data.get("log", {}).get("entries", []))
        except Exception:
            pass

    return HarResult(url=url, har_file=har_path, entry_count=entry_count)


def playback_har(
    url: str,
    har_file: Path,
    output_dir: Path,
    update_missing: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> BrowseResult:
    """Replay a HAR file: serve recorded responses instead of hitting the network.

    update_missing: if True, falls back to network for requests not in HAR.
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    shot_path = output_dir / f"{stem}_har_replay.png"
    html_path = output_dir / f"{stem}_har_replay.html"

    not_found_action = "continue" if update_missing else "abort"

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.route_from_har(str(har_file), not_found=not_found_action)
        page = ctx.new_page()
        page.goto(url, timeout=60_000)
        title = page.title()
        page.screenshot(path=str(shot_path), full_page=True)
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
        browser.close()

    return BrowseResult(
        url=url,
        output_dir=output_dir,
        screenshot=shot_path,
        html_file=html_path,
        title=title,
    )


# ---------------------------------------------------------------------------
# 4. Screenshot advanced  (JPEG, mask, scale, omit_background, animations)
# ---------------------------------------------------------------------------

@dataclass
class ScreenshotResult:
    url: str
    output_dir: Path
    file: Path | None = None
    buffer: bytes | None = None
    format: str = "png"

    def report(self) -> str:
        lines = [f"Screenshot: {self.url}"]
        if self.file:
            lines.append(f"  File: {self.file}  ({self.file.stat().st_size} bytes)")
        lines.append(f"  Format: {self.format}")
        return "\n".join(lines)


def screenshot_advanced(
    url: str,
    output_dir: Path,
    fmt: str = "png",
    quality: int | None = None,
    full_page: bool = True,
    clip: dict | None = None,
    selector: str | None = None,
    mask_selectors: list[str] | None = None,
    omit_background: bool = False,
    animations: str = "allow",
    scale: str = "device",
    to_buffer: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> ScreenshotResult:
    """Take a screenshot with full control over all Playwright options.

    fmt: "png" or "jpeg"
    quality: JPEG quality 0-100 (ignored for PNG)
    clip: {"x": int, "y": int, "width": int, "height": int}
    mask_selectors: list of CSS selectors to mask (replaced with pink boxes)
    omit_background: capture transparent background (PNG only)
    animations: "allow" | "disabled" | "initial"
    scale: "device" | "css"
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    ext = "jpg" if fmt == "jpeg" else "png"
    file_path = output_dir / f"{stem}_adv.{ext}"

    shot_kwargs: dict = {
        "full_page": full_page,
        "type": fmt,
        "animations": animations,
        "scale": scale,
    }
    if clip:
        shot_kwargs["clip"] = clip
        shot_kwargs.pop("full_page", None)
    if omit_background and fmt == "png":
        shot_kwargs["omit_background"] = True
    if quality is not None and fmt == "jpeg":
        shot_kwargs["quality"] = quality
    if mask_selectors:
        shot_kwargs["mask"] = [f"css={s}" for s in mask_selectors]

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if selector:
            loc = page.locator(selector).first
            if to_buffer:
                buf = loc.screenshot(**shot_kwargs)
                browser.close()
                return ScreenshotResult(url=url, output_dir=output_dir, buffer=buf, format=fmt)
            else:
                loc.screenshot(path=str(file_path), **shot_kwargs)
        else:
            if to_buffer:
                buf = page.screenshot(**shot_kwargs)
                browser.close()
                return ScreenshotResult(url=url, output_dir=output_dir, buffer=buf, format=fmt)
            else:
                page.screenshot(path=str(file_path), **shot_kwargs)

        browser.close()

    return ScreenshotResult(url=url, output_dir=output_dir, file=file_path, format=fmt)


# ---------------------------------------------------------------------------
# 5. PDF advanced  (margins, header/footer, page_ranges, landscape)
# ---------------------------------------------------------------------------

@dataclass
class PdfResult:
    url: str
    output_dir: Path
    file: Path

    def report(self) -> str:
        return (
            f"PDF: {self.url}\n"
            f"  File: {self.file}\n"
            f"  Size: {self.file.stat().st_size} bytes"
        )


def generate_pdf(
    url: str,
    output_dir: Path,
    fmt: str = "A4",
    landscape: bool = False,
    print_background: bool = True,
    margin: dict | None = None,
    header_template: str | None = None,
    footer_template: str | None = None,
    page_ranges: str | None = None,
    scale: float = 1.0,
    prefer_css_page_size: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> PdfResult:
    """Generate a PDF from a URL with full Playwright pdf() option support.

    margin: {"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"}
    header_template: HTML string for page header (use <span class="pageNumber">)
    footer_template: HTML string for page footer
    page_ranges: e.g. "1-3, 5" to select specific pages
    scale: 0.1 - 2.0
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    pdf_path = output_dir / f"{stem}.pdf"

    pdf_kwargs: dict = {
        "path": str(pdf_path),
        "format": fmt,
        "landscape": landscape,
        "print_background": print_background,
        "scale": scale,
        "prefer_css_page_size": prefer_css_page_size,
    }
    if margin:
        pdf_kwargs["margin"] = margin
    if header_template or footer_template:
        pdf_kwargs["display_header_footer"] = True
        pdf_kwargs["header_template"] = header_template or ""
        pdf_kwargs["footer_template"] = footer_template or ""
    if page_ranges:
        pdf_kwargs["page_ranges"] = page_ranges

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.pdf(**pdf_kwargs)
        browser.close()

    return PdfResult(url=url, output_dir=output_dir, file=pdf_path)


# ---------------------------------------------------------------------------
# 6. Clock API  (freeze/advance browser time)
# ---------------------------------------------------------------------------

@dataclass
class ClockResult:
    url: str
    fixed_time: str | None
    output_dir: Path
    screenshot: Path | None = None
    evaluated: Any = None

    def report(self) -> str:
        lines = [f"Clock: {self.url}"]
        if self.fixed_time:
            lines.append(f"  Fixed time: {self.fixed_time}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        return "\n".join(lines)


def browse_with_clock(
    url: str,
    output_dir: Path,
    fixed_time: str | None = None,
    tick_ms: int | None = None,
    run_for_ms: int | None = None,
    eval_script: str | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
) -> ClockResult:
    """Browse a URL with the browser clock frozen or advanced.

    fixed_time: ISO-8601 string or timestamp ms to freeze the clock at.
    tick_ms: advance the clock by this many ms after page load.
    run_for_ms: run all timers for this many ms (fires setTimeout/setInterval).
    eval_script: optional JS to evaluate after clock operations.
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    shot_path = output_dir / f"{stem}_clock.png"

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        if fixed_time:
            page.clock.set_fixed_time(fixed_time)
        elif tick_ms is not None or run_for_ms is not None:
            page.clock.install()

        page.goto(url, wait_until="networkidle", timeout=60_000)

        if tick_ms is not None:
            page.clock.tick(tick_ms)
        if run_for_ms is not None:
            page.clock.run_for(run_for_ms)

        eval_result = None
        if eval_script:
            eval_result = page.evaluate(eval_script)

        page.screenshot(path=str(shot_path), full_page=True)
        browser.close()

    return ClockResult(
        url=url,
        fixed_time=fixed_time,
        output_dir=output_dir,
        screenshot=shot_path,
        evaluated=eval_result,
    )


# ---------------------------------------------------------------------------
# 7. Cookie management
# ---------------------------------------------------------------------------

@dataclass
class CookieResult:
    url: str
    cookies: list[dict]
    output_file: Path | None = None

    def report(self) -> str:
        lines = [f"Cookies: {self.url}  ({len(self.cookies)} total)"]
        for c in self.cookies[:10]:
            lines.append(f"  {c.get('name')}={str(c.get('value',''))[:40]}  (domain={c.get('domain','')})")
        return "\n".join(lines)


def manage_cookies(
    url: str,
    output_dir: Path,
    add_cookies: list[dict] | None = None,
    clear: bool = False,
    browser_name: str = "chromium",
    headless: bool = True,
) -> CookieResult:
    """Get, add, or clear cookies for a URL's browsing context.

    add_cookies: list of cookie dicts with keys: name, value, domain, path, etc.
    clear: if True, clears all cookies before loading.
    Returns all cookies after the page load.
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})

        if clear:
            ctx.clear_cookies()

        if add_cookies:
            ctx.add_cookies(add_cookies)

        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)

        cookies = ctx.cookies()
        browser.close()

    stem = _safe_filename(url)
    out_file = output_dir / f"{stem}_cookies.json"
    out_file.write_text(
        json.dumps(cookies, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    return CookieResult(url=url, cookies=cookies, output_file=out_file)


# ---------------------------------------------------------------------------
# 8. File chooser
# ---------------------------------------------------------------------------

@dataclass
class FileChooserResult:
    url: str
    trigger_selector: str
    files_set: list[str]
    screenshot: Path | None = None

    def report(self) -> str:
        lines = [f"File chooser: {self.url}"]
        lines.append(f"  Trigger: {self.trigger_selector}")
        lines.append(f"  Files set: {self.files_set}")
        if self.screenshot:
            lines.append(f"  Screenshot: {self.screenshot}")
        return "\n".join(lines)


def handle_file_chooser(
    url: str,
    trigger_selector: str,
    files: list[str | Path],
    output_dir: Path,
    browser_name: str = "chromium",
    headless: bool = True,
) -> FileChooserResult:
    """Click an element that opens a file chooser and inject files programmatically.

    trigger_selector: CSS selector for the element that opens the chooser.
    files: list of file paths to inject into the chooser.
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(url)
    shot_path = output_dir / f"{stem}_filechooser.png"
    file_strs = [str(f) for f in files]

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)

        with page.expect_file_chooser() as fc_info:
            page.click(trigger_selector)
        file_chooser = fc_info.value
        file_chooser.set_files(file_strs)

        page.screenshot(path=str(shot_path), full_page=True)
        browser.close()

    return FileChooserResult(
        url=url,
        trigger_selector=trigger_selector,
        files_set=file_strs,
        screenshot=shot_path,
    )


# ---------------------------------------------------------------------------
# 9. WebSockets monitoring
# ---------------------------------------------------------------------------

@dataclass
class WebSocketResult:
    url: str
    ws_url: str | None
    frames_received: list[dict]
    frames_sent: list[dict]
    output_file: Path | None = None

    def report(self) -> str:
        lines = [f"WebSocket monitor: {self.url}"]
        if self.ws_url:
            lines.append(f"  WS endpoint: {self.ws_url}")
        lines.append(f"  Frames received: {len(self.frames_received)}")
        lines.append(f"  Frames sent:     {len(self.frames_sent)}")
        return "\n".join(lines)


def monitor_websockets(
    url: str,
    output_dir: Path,
    wait_ms: int = 5000,
    browser_name: str = "chromium",
    headless: bool = True,
) -> WebSocketResult:
    """Load a URL and capture all WebSocket frames (sent and received).

    wait_ms: how long to wait after page load for WS traffic (ms).
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    import time as _time

    frames_received: list[dict] = []
    frames_sent: list[dict] = []
    ws_url: str | None = None
    lock = threading.Lock()

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def _on_websocket(ws):
            nonlocal ws_url
            with lock:
                ws_url = ws.url

            def _on_frame_recv(payload):
                with lock:
                    frames_received.append({"payload": str(payload)[:500], "ws": ws.url})

            def _on_frame_sent(payload):
                with lock:
                    frames_sent.append({"payload": str(payload)[:500], "ws": ws.url})

            ws.on("framereceived", _on_frame_recv)
            ws.on("framesent", _on_frame_sent)

        page.on("websocket", _on_websocket)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        _time.sleep(wait_ms / 1000)
        browser.close()

    stem = _safe_filename(url)
    out_file = output_dir / f"{stem}_websockets.json"
    out_file.write_text(
        json.dumps({
            "url": url,
            "ws_url": ws_url,
            "frames_received": frames_received,
            "frames_sent": frames_sent,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return WebSocketResult(
        url=url,
        ws_url=ws_url,
        frames_received=frames_received,
        frames_sent=frames_sent,
        output_file=out_file,
    )


# ---------------------------------------------------------------------------
# 10. Service Workers + expose_binding + pageerror + requestfailed
# ---------------------------------------------------------------------------

@dataclass
class PageEventsResult:
    url: str
    page_errors: list[str]
    request_failures: list[dict]
    service_workers: list[str]
    console_messages: list[dict]
    output_file: Path | None = None

    def report(self) -> str:
        lines = [f"Page events: {self.url}"]
        lines.append(f"  Page errors:      {len(self.page_errors)}")
        lines.append(f"  Request failures: {len(self.request_failures)}")
        lines.append(f"  Service workers:  {len(self.service_workers)}")
        lines.append(f"  Console msgs:     {len(self.console_messages)}")
        return "\n".join(lines)


def monitor_page_events(
    url: str,
    output_dir: Path,
    expose_binding_name: str | None = None,
    expose_binding_fn: Callable | None = None,
    browser_name: str = "chromium",
    headless: bool = True,
) -> PageEventsResult:
    """Load a URL and capture page errors, request failures, service workers, and console.

    expose_binding_name/fn: expose a Python function to JS as window.<name>(source, ...args).
    """
    _require_playwright()
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    page_errors: list[str] = []
    request_failures: list[dict] = []
    console_messages: list[dict] = []

    with sync_playwright() as p:
        browser = _get_browser(p, browser_name, headless=headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})

        if expose_binding_name and expose_binding_fn:
            ctx.expose_binding(expose_binding_name, expose_binding_fn)

        page = ctx.new_page()

        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("requestfailed", lambda req: request_failures.append({
            "url": req.url,
            "method": req.method,
            "failure": req.failure or "",
        }))
        page.on("console", lambda msg: console_messages.append({
            "type": msg.type,
            "text": msg.text,
        }))

        page.goto(url, wait_until="networkidle", timeout=60_000)

        # Collect service workers registered by this context
        sw_urls = [sw.url for sw in ctx.service_workers()]

        browser.close()

    stem = _safe_filename(url)
    out_file = output_dir / f"{stem}_events.json"
    out_file.write_text(
        json.dumps({
            "url": url,
            "page_errors": page_errors,
            "request_failures": request_failures,
            "service_workers": sw_urls,
            "console_messages": console_messages,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return PageEventsResult(
        url=url,
        page_errors=page_errors,
        request_failures=request_failures,
        service_workers=sw_urls,
        console_messages=console_messages,
        output_file=out_file,
    )
