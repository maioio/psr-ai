"""Full-page capture: screenshot + PDF generation using Playwright."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse


def capture_page(url: str, output_path: Path, as_pdf: bool = False) -> Path:
    """Capture a full-page screenshot or PDF from a URL."""
    from playwright.sync_api import sync_playwright

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        if as_pdf:
            page.pdf(path=str(output_path), format="A4", print_background=True)
        else:
            page.screenshot(path=str(output_path), full_page=True)

        browser.close()

    return output_path


# ---------------------------------------------------------------------------
# GitHub-specific page list
# ---------------------------------------------------------------------------

def _github_repo_pages(base_url: str) -> list[tuple[str, str]]:
    """Return (label, url) pairs for all meaningful pages of a GitHub repo."""
    base_url = base_url.rstrip("/")
    m = re.match(r"(https?://github\.com/[^/]+/[^/]+)", base_url)
    if not m:
        return [("page", base_url)]
    root = m.group(1)
    return [
        ("README / Code",       root),
        ("File tree",           f"{root}/tree/HEAD"),
        ("Commits",             f"{root}/commits"),
        ("Branches",            f"{root}/branches"),
        ("Releases",            f"{root}/releases"),
        ("Issues",              f"{root}/issues"),
        ("Pull requests",       f"{root}/pulls"),
        ("Actions / Workflows", f"{root}/actions"),
        ("Wiki",                f"{root}/wiki"),
        ("Insights",            f"{root}/graphs/contributors"),
    ]


# ---------------------------------------------------------------------------
# Generic site crawler
# ---------------------------------------------------------------------------

def _collect_nav_links(
    browser_ctx,
    start_url: str,
    intent: str | None,
    max_pages: int,
    delay: float,
    echo_fn,
) -> list[tuple[str, str]]:
    """Load the start URL, find internal links, and return (label, url) pairs.

    If `intent` is given, it is used only to filter/prioritise links that
    seem relevant (simple keyword match on href and link text).
    The home page itself is always included first.
    """
    origin = urlparse(start_url).scheme + "://" + urlparse(start_url).netloc

    page = browser_ctx.new_page()
    try:
        page.goto(start_url, wait_until="networkidle", timeout=45_000)
        if delay > 0:
            time.sleep(delay)

        # Collect all <a href> tags that point within the same domain.
        anchors = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(a => ({
                href: a.href,
                text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80)
            }))""",
        )
    finally:
        page.close()

    seen: set[str] = {start_url.rstrip("/")}
    candidates: list[tuple[str, str]] = [("Home", start_url)]

    keywords = [k.lower() for k in (intent or "").split()] if intent else []

    # Score links: keyword matches first, then order of appearance.
    scored: list[tuple[int, str, str]] = []
    for a in anchors:
        href: str = a.get("href", "")
        text: str = a.get("text", "") or ""

        # Must be same origin, not a file download, not an anchor-only link.
        parsed = urlparse(href)
        if parsed.netloc and not href.startswith(origin):
            continue
        if not parsed.path or parsed.path in ("/", ""):
            continue
        if re.search(r"\.(zip|tar|gz|pdf|png|jpg|svg|ico|woff|css|js)$", parsed.path, re.I):
            continue

        # Normalise
        full = urljoin(origin, parsed.path).rstrip("/")
        if full in seen:
            continue
        seen.add(full)

        score = 0
        if keywords:
            combined = (href + " " + text).lower()
            score = sum(1 for kw in keywords if kw in combined)

        label = text if text else parsed.path.strip("/").replace("/", " › ") or href
        scored.append((-score, label, full))   # negative so higher score sorts first

    scored.sort(key=lambda x: x[0])
    for _, label, href in scored:
        if len(candidates) >= max_pages:
            break
        candidates.append((label, href))

    return candidates


def crawl_site(
    url: str,
    output_path: Path,
    intent: str | None = None,
    max_pages: int = 15,
    echo_fn=None,
    delay: float = 1.5,
) -> Path:
    """Capture multiple pages of any website into one merged PDF.

    Behaviour:
      - GitHub repos get a curated page list (README, commits, issues, …).
      - All other sites: load the home page, collect internal nav links,
        optionally filter by `intent` keywords, then capture up to `max_pages`.

    Args:
        url:         Starting URL.
        output_path: Destination .pdf file.
        intent:      Optional free-text description of what matters (used to
                     prioritise which links to follow).
        max_pages:   Maximum number of pages to capture (default 15).
        echo_fn:     Optional callable(str) for progress messages.
        delay:       Seconds to wait for JS to settle after each navigation.
    """
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise ImportError(
            "pypdf is required for --crawl. Install it with: pip install pypdf"
        )
    from playwright.sync_api import sync_playwright

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_path.parent / (output_path.stem + "_tmp_pages")
    tmp_dir.mkdir(exist_ok=True)

    is_github_repo = bool(re.match(r"https?://github\.com/[^/]+/[^/]+", url))

    captured: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})

        # Determine which pages to capture.
        if is_github_repo:
            pages = _github_repo_pages(url)
            _echo(f"  GitHub repo detected — using curated page list ({len(pages)} pages)")
        else:
            _echo("  Scanning site navigation...")
            pages = _collect_nav_links(ctx, url, intent, max_pages, delay, echo_fn)
            _echo(f"  Found {len(pages)} pages to capture")

        for idx, (label, page_url) in enumerate(pages):
            _echo(f"  [{idx + 1}/{len(pages)}] {label}")
            bpage = ctx.new_page()
            try:
                resp = bpage.goto(page_url, wait_until="domcontentloaded", timeout=45_000)

                if resp and resp.status >= 400:
                    _echo(f"    Skipped (HTTP {resp.status})")
                    bpage.close()
                    continue

                # GitHub redirects back to root when JS hasn't loaded the tab yet.
                # Wait longer and confirm we actually landed on the right path.
                if is_github_repo:
                    time.sleep(3.0)
                    expected_path = urlparse(page_url).path.rstrip("/")
                    actual_path = urlparse(bpage.url).path.rstrip("/")
                    if expected_path and expected_path != actual_path:
                        _echo(f"    Skipped (redirected to {actual_path})")
                        bpage.close()
                        continue
                else:
                    # For non-GitHub sites, skip if we landed outside the origin.
                    origin = urlparse(url).netloc
                    if urlparse(bpage.url).netloc != origin:
                        _echo(f"    Skipped (redirected off-site)")
                        bpage.close()
                        continue
                    if delay > 0:
                        time.sleep(delay)

                safe_label = re.sub(r"[^\w\-]", "_", label)[:50]
                tmp_pdf = tmp_dir / f"{idx:03d}_{safe_label}.pdf"
                bpage.pdf(path=str(tmp_pdf), format="A4", print_background=True)
                captured.append(tmp_pdf)
                _echo(f"    OK")
            except Exception as exc:
                _echo(f"    Skipped ({exc})")
            finally:
                bpage.close()

        browser.close()

    if not captured:
        raise RuntimeError("No pages could be captured. Check the URL and your network.")

    _echo(f"  Merging {len(captured)} pages into PDF...")
    writer = PdfWriter()
    for pdf_path in sorted(captured):
        writer.append(str(pdf_path))

    with open(output_path, "wb") as f:
        writer.write(f)

    for pdf_path in captured:
        try:
            pdf_path.unlink()
        except OSError:
            pass
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    return output_path


# Keep old name as alias so existing callers don't break.
crawl_repo = crawl_site
