"""Extract HTML of clicked elements from browser sessions recorded by PSR.

When sa stop produces a session where Chrome/Edge was active, this module
re-opens the same URL with Playwright (headless) and pulls the outer HTML
of every element that was clicked during the session. The HTML is added to
each step in data.json as 'element_html', giving Claude exact DOM context
instead of just a screenshot.

This is the "DevTools inspect" tip from the video — automated:
  instead of: right-click → Inspect → copy HTML → paste to Claude
  PSR.ai does: sa stop → element HTML already in the report
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ElementHTML:
    step_index: int
    url: str
    selector_hint: str      # the UI element name from PSR
    html: str               # outer HTML of the element
    error: str = ""


# ---------------------------------------------------------------------------
# URL extraction from window titles
# ---------------------------------------------------------------------------

_CHROME_TITLE_RE = re.compile(
    r"^(.+?)\s*[-–]\s*(?:Google Chrome|Microsoft Edge|Mozilla Firefox)$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _extract_url_from_title(title: str) -> str | None:
    """Try to get a URL from a Chrome/Edge window title."""
    # Sometimes the full URL appears in the title (e.g. VS Code embedded browser)
    m = _URL_RE.search(title)
    if m:
        return m.group(0)
    return None


def _is_browser_step(step: dict) -> bool:
    prog = (step.get("program") or "").lower()
    return any(b in prog for b in ("chrome", "edge", "firefox", "browser"))


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_element_html(
    steps: list[dict],
    echo: Callable[[str], None] | None = None,
) -> list[ElementHTML]:
    """For each browser click step, fetch the element's HTML via Playwright.

    Only runs if playwright is installed. Silently skips if not.
    Returns a list of ElementHTML, one per successfully extracted element.
    """
    def _echo(msg: str) -> None:
        if echo:
            echo(msg)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    # Filter to browser click steps that have a UI element name
    targets = [
        s for s in steps
        if _is_browser_step(s)
        and "click" in (s.get("action_text") or "").lower()
        and s.get("ui_element")
    ]
    if not targets:
        return []

    # Group by URL (window title → URL)
    url_groups: dict[str, list[dict]] = {}
    for step in targets:
        title = step.get("ui_element") or ""
        url = _extract_url_from_title(title)
        if not url:
            # Try the action_text for URL hints
            m = _URL_RE.search(step.get("action_text") or "")
            url = m.group(0) if m else None
        if url:
            url_groups.setdefault(url, []).append(step)

    if not url_groups:
        _echo("  HTML extraction: no browser URLs found in session.")
        return []

    results: list[ElementHTML] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        for url, url_steps in list(url_groups.items())[:3]:  # max 3 URLs per session
            try:
                _echo(f"  HTML extraction: loading {url[:60]}...")
                page.goto(url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception as exc:
                _echo(f"  HTML extraction: failed to load {url[:60]}: {exc}")
                continue

            for step in url_steps:
                element_name = step.get("ui_element") or ""
                # Strip accessibility type hints like "(לחצן)", "(עריכה)" etc.
                clean_name = re.sub(r"\s*\([֐-׿\w]+\)\s*$", "", element_name).strip()
                if not clean_name:
                    continue

                html = _locate_element(page, clean_name)
                results.append(ElementHTML(
                    step_index=step.get("index", -1),
                    url=url,
                    selector_hint=clean_name,
                    html=html or "",
                    error="" if html else "element not found",
                ))

        browser.close()

    found = sum(1 for r in results if r.html)
    _echo(f"  HTML extraction: {found}/{len(results)} elements extracted.")
    return results


def _locate_element(page, name: str) -> str | None:
    """Try multiple strategies to find an element and return its outerHTML."""
    from playwright.sync_api import Page

    strategies = [
        # Exact text match
        lambda: page.get_by_text(name, exact=True).first,
        # Partial text match
        lambda: page.get_by_text(name).first,
        # Role-based: button, link, input
        lambda: page.get_by_role("button", name=name).first,
        lambda: page.get_by_role("link", name=name).first,
        lambda: page.get_by_label(name).first,
        lambda: page.get_by_placeholder(name).first,
    ]

    for strategy in strategies:
        try:
            locator = strategy()
            if locator.count() > 0:
                html = locator.evaluate("el => el.outerHTML")
                if html and len(html) < 4000:  # skip enormous elements
                    return html
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Inject HTML back into session steps
# ---------------------------------------------------------------------------

def enrich_steps_with_html(
    steps: list[dict],
    echo: Callable[[str], None] | None = None,
) -> int:
    """Run extraction and write element_html into matching steps in-place.

    Returns the number of steps enriched.
    """
    extractions = extract_element_html(steps, echo=echo)
    if not extractions:
        return 0

    index_map = {e.step_index: e for e in extractions if e.html}
    for step in steps:
        idx = step.get("index", -1)
        if idx in index_map:
            step["element_html"] = index_map[idx].html

    return len(index_map)
