"""Validate the built documentation site in site/dist.

Checks: base path, internal links and assets resolve to files, #fragments
resolve to an id on the target page, canonical URLs, no stale repository URLs,
no paid-tier call to action. Exit 1 on any failure.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "site" / "dist"
BASE = "/psr-ai"
SITE = "https://maioio.github.io/psr-ai"
BANNED = [
    (re.compile(r"maioio-projects", re.I), "stale repository URL"),
    (re.compile(r"\bGet Pro\b|\bGumroad\b|\$9\s*/\s*mo", re.I), "paid-tier call to action"),
    (re.compile(r"Enterprise Cloud", re.I), "unshipped hosted tier"),
    (re.compile(r"\bup to 80\s*%", re.I), "unverified savings claim"),
]


class Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.canonical: str | None = None
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        ident, href, src = a.get("id"), a.get("href"), a.get("src")
        if ident:
            self.ids.add(ident)
        for ref in (href, src):
            if ref:
                self.refs.append(ref)
        if tag == "link" and a.get("rel") == "canonical":
            self.canonical = href


def resolve(ref: str, page: Path) -> Path | None:
    path = unquote(urlparse(ref).path)
    if path.startswith(BASE + "/") or path == BASE:
        target = DIST / path[len(BASE):].lstrip("/")
    elif path.startswith("/"):
        return None  # root-absolute without the base path
    else:
        target = (page.parent / path).resolve()
    if target.is_dir():
        target = target / "index.html"
    return target


_parsed: dict[Path, Refs] = {}


def parse(page: Path) -> Refs:
    if page not in _parsed:
        p = Refs()
        p.feed(page.read_text(encoding="utf-8", errors="replace"))
        _parsed[page] = p
    return _parsed[page]


def check_ref(ref: str, page: Path) -> str | None:
    u = urlparse(ref)
    if u.scheme in ("http", "https", "mailto", "data", "javascript") or ref.startswith("//"):
        return None
    if ref.startswith("#"):
        target = page
    else:
        target = resolve(ref, page)
        if target is None:
            return f"root-absolute link without base path: {ref}"
        if not target.exists():
            return f"broken internal reference: {ref}"
    frag = unquote(u.fragment)
    if frag and target.suffix == ".html" and frag not in parse(target).ids:
        return f"missing anchor: {ref}"
    return None


def main() -> int:
    if not DIST.is_dir():
        print("site/dist missing: run `npm run build` in site/")
        return 1
    errors: list[str] = []
    pages = sorted(DIST.rglob("*.html"))
    for page in pages:
        rel = page.relative_to(DIST).as_posix()
        text = page.read_text(encoding="utf-8", errors="replace")
        for rx, why in BANNED:
            if rx.search(text):
                errors.append(f"{rel}: {why}")
        p = parse(page)
        if p.canonical and not p.canonical.startswith(SITE):
            errors.append(f"{rel}: canonical {p.canonical} not under {SITE}")
        for ref in p.refs:
            problem = check_ref(ref, page)
            if problem:
                errors.append(f"{rel}: {problem}")
    for need in ("sitemap-index.xml", "robots.txt"):
        if not (DIST / need).exists():
            errors.append(f"missing {need}")
    print(f"checked {len(pages)} pages")
    for e in errors:
        print("FAIL", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
