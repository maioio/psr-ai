"""Scientific species lookup: iNaturalist, GBIF, and BugGuide."""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SpeciesResult:
    query: str
    output_dir: Path | None
    inat: list[dict] = field(default_factory=list)
    gbif: list[dict] = field(default_factory=list)
    bugguide: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.inat) + len(self.gbif) + len(self.bugguide)

    def report(self) -> str:
        if self.error:
            return f"Species lookup failed: {self.error}"
        lines = [f"Species results for: {self.query}"]
        if self.inat:
            lines.append(f"\niNaturalist ({len(self.inat)} results):")
            for r in self.inat[:5]:
                name = r.get("name", "")
                common = r.get("preferred_common_name", "")
                obs = r.get("observations_count", 0)
                label = f"  {name}"
                if common:
                    label += f" ({common})"
                if obs:
                    label += f"  - {obs:,} observations"
                lines.append(label)
        if self.gbif:
            lines.append(f"\nGBIF ({len(self.gbif)} results):")
            for r in self.gbif[:5]:
                name = r.get("scientificName", r.get("canonicalName", ""))
                rank = r.get("rank", "")
                status = r.get("status", "")
                label = f"  {name}"
                if rank:
                    label += f" [{rank}]"
                if status:
                    label += f" - {status}"
                lines.append(label)
        if self.bugguide:
            lines.append(f"\nBugGuide ({len(self.bugguide)} results):")
            for r in self.bugguide[:5]:
                lines.append(f"  {r.get('title', '')} - {r.get('url', '')}")
        if self.output_dir:
            lines.append(f"\nData saved to: {self.output_dir}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "User-Agent": "PSR.ai/1.0 (https://github.com/maioio/psr-ai; species research tool)",
    "Accept": "application/json",
}


def _get_json(url: str, params: dict | None = None, timeout: int = 20) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# iNaturalist
# ---------------------------------------------------------------------------

def _inat_search(query: str, limit: int = 10) -> list[dict]:
    """Search iNaturalist taxa."""
    data = _get_json(
        "https://api.inaturalist.org/v1/taxa",
        params={"q": query, "per_page": limit, "rank": "species,genus,family"},
    )
    results = []
    for taxon in data.get("results", []):
        results.append({
            "name": taxon.get("name", ""),
            "preferred_common_name": taxon.get("preferred_common_name", ""),
            "rank": taxon.get("rank", ""),
            "observations_count": taxon.get("observations_count", 0),
            "wikipedia_url": taxon.get("wikipedia_url", ""),
            "inat_url": f"https://www.inaturalist.org/taxa/{taxon.get('id', '')}",
            "default_photo": (taxon.get("default_photo") or {}).get("medium_url", ""),
        })
    return results


def _inat_observations(taxon_id: int, limit: int = 5) -> list[dict]:
    """Fetch recent observations for a taxon ID."""
    data = _get_json(
        "https://api.inaturalist.org/v1/observations",
        params={
            "taxon_id": taxon_id,
            "per_page": limit,
            "order_by": "observed_on",
            "order": "desc",
        },
    )
    results = []
    for obs in data.get("results", []):
        results.append({
            "id": obs.get("id"),
            "observed_on": obs.get("observed_on", ""),
            "place_guess": obs.get("place_guess", ""),
            "quality_grade": obs.get("quality_grade", ""),
            "url": f"https://www.inaturalist.org/observations/{obs.get('id', '')}",
        })
    return results


# ---------------------------------------------------------------------------
# GBIF
# ---------------------------------------------------------------------------

def _gbif_search(query: str, limit: int = 10) -> list[dict]:
    """Search GBIF species names."""
    data = _get_json(
        "https://api.gbif.org/v1/species/search",
        params={"q": query, "limit": limit},
    )
    results = []
    for rec in data.get("results", []):
        results.append({
            "scientificName": rec.get("scientificName", ""),
            "canonicalName": rec.get("canonicalName", ""),
            "rank": rec.get("rank", ""),
            "status": rec.get("taxonomicStatus", ""),
            "kingdom": rec.get("kingdom", ""),
            "phylum": rec.get("phylum", ""),
            "class": rec.get("class_", rec.get("clazz", "")),
            "order": rec.get("order", ""),
            "family": rec.get("family", ""),
            "genus": rec.get("genus", ""),
            "gbif_key": rec.get("key"),
            "gbif_url": f"https://www.gbif.org/species/{rec.get('key', '')}",
        })
    return results


# ---------------------------------------------------------------------------
# BugGuide
# ---------------------------------------------------------------------------

def _bugguide_search(query: str, limit: int = 10) -> list[dict]:
    """Search BugGuide via their search endpoint (HTML scrape, no official API)."""
    try:
        from playwright.sync_api import sync_playwright
        return _bugguide_playwright(query, limit)
    except ImportError:
        pass

    # Fallback: use urllib with a basic HTML parse
    return _bugguide_urllib(query, limit)


def _bugguide_urllib(query: str, limit: int) -> list[dict]:
    """BugGuide search via urllib with basic HTML extraction."""
    search_url = (
        "https://bugguide.net/index.php?q=search&"
        + urllib.parse.urlencode({"keys": query, "search": "Search"})
    )
    headers = {
        "User-Agent": "PSR.ai/1.0 species research tool",
        "Accept": "text/html",
    }
    req = urllib.request.Request(search_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    results: list[dict] = []
    # Extract links that look like taxa or guide pages
    for m in re.finditer(
        r'<a\s+href="(https://bugguide\.net/node/view/\d+)"[^>]*>([^<]{3,80})</a>',
        html,
    ):
        href, title = m.group(1), m.group(2).strip()
        if title and href not in {r["url"] for r in results}:
            results.append({"title": title, "url": href})
        if len(results) >= limit:
            break
    return results


def _bugguide_playwright(query: str, limit: int) -> list[dict]:
    """BugGuide search via headless Playwright for JS-rendered results."""
    from playwright.sync_api import sync_playwright

    search_url = (
        "https://bugguide.net/index.php?q=search&"
        + urllib.parse.urlencode({"keys": query, "search": "Search"})
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

        anchors = page.eval_on_selector_all(
            'a[href*="bugguide.net/node/view"]',
            "els => els.map(a => ({href: a.href, text: a.innerText.trim().slice(0,100)}))",
        )
        browser.close()

    results: list[dict] = []
    seen: set[str] = set()
    for a in anchors:
        href = a.get("href", "")
        text = a.get("text", "").strip()
        if text and href and href not in seen:
            seen.add(href)
            results.append({"title": text, "url": href})
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def lookup_species(
    query: str,
    output_dir: Path | None = None,
    sources: list[str] | None = None,
    limit: int = 10,
) -> SpeciesResult:
    """Search iNaturalist, GBIF, and/or BugGuide for a species or common name.

    Args:
        query:      Scientific or common name to search.
        output_dir: If provided, save JSON results here.
        sources:    List of sources to query: ["inat", "gbif", "bugguide"].
                    Defaults to all three.
        limit:      Max results per source.
    """
    if sources is None:
        sources = ["inat", "gbif", "bugguide"]

    inat_results: list[dict] = []
    gbif_results: list[dict] = []
    bugguide_results: list[dict] = []
    error: str | None = None

    try:
        if "inat" in sources:
            inat_results = _inat_search(query, limit=limit)
        if "gbif" in sources:
            gbif_results = _gbif_search(query, limit=limit)
        if "bugguide" in sources:
            bugguide_results = _bugguide_search(query, limit=limit)
    except Exception as exc:
        error = str(exc)

    result = SpeciesResult(
        query=query,
        output_dir=output_dir,
        inat=inat_results,
        gbif=gbif_results,
        bugguide=bugguide_results,
        error=error,
    )

    if output_dir and not error:
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_q = re.sub(r"[^\w\-]", "_", query)[:50]
        data_path = output_dir / f"{safe_q}_species.json"
        data_path.write_text(
            json.dumps(
                {
                    "query": query,
                    "inat": inat_results,
                    "gbif": gbif_results,
                    "bugguide": bugguide_results,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    return result
