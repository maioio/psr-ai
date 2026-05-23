"""Parses PSR output: reads the MHT entirely into memory, extracts PNG steps in parallel.

PSR MHT format: the recording metadata lives in a <script id="myXML" type="text/xml">
block embedded in the HTML part. Each <EachAction> element contains:
  - ActionNumber, Time attributes on the tag
  - <Description> - Hebrew/localized human-readable action description
  - <Action> - action type (left click, scroll, etc.)
  - <CursorCoordsXY> - click coordinates
  - FileDescription attribute - the program name (e.g. "Google Chrome")
  - <ScreenshotFileName> - which screenshot corresponds to this action
  - <UIAStack><Level Name="..."> - the UI element name

_extract_step_metadata_xml() parses this directly.
The old HTML regex fallback (_extract_step_metadata) is kept but used only when no
XML block is found.
"""

from __future__ import annotations

import base64
import concurrent.futures
import email
import email.policy
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from psr_ai.utils.encoding import decode_mht_bytes, read_mht_binary
from psr_ai.utils.paths import safe_path, default_output_dir


# ---------------------------------------------------------------------------
# Pre-compiled patterns (compiled once at import time for zero per-call cost)
# ---------------------------------------------------------------------------

# PSR step heading: <p class="step">Step N: <b>Action text</b></p>
_RE_STEP = re.compile(
    r'<[^>]*class=["\']?step["\']?[^>]*>\s*'
    r'(?:Step\s*(\d+)\s*[:\-])?\s*'
    r'(?:<b[^>]*>([^<]*)</b>)?',
    re.IGNORECASE,
)

# Timestamp inside PSR HTML: M/D/YYYY H:MM:SS AM/PM
_RE_TIMESTAMP = re.compile(
    r'(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?)',
    re.IGNORECASE,
)

# Program / window title
_RE_PROGRAM = re.compile(
    r'(?:window title|program):\s*([^\n<]+)',
    re.IGNORECASE,
)

# Fallback <dd> blocks used by some PSR versions
_RE_DD = re.compile(r'<dd[^>]*>(.*?)</dd>', re.IGNORECASE | re.DOTALL)

# HTML tag stripper (used inside _RE_DD fallback)
_RE_TAGS = re.compile(r'<[^>]+>')

# <title> extraction
_RE_TITLE = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE)

# Base64 whitespace stripper (PSR inserts CRLF every 76 chars)
_RE_WS = re.compile(r'\s+')


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """A single recorded step: screenshot bytes and descriptive text."""

    index: int
    label: str
    image_bytes: bytes
    timestamp: str = ""
    ui_element: str = ""
    program: str = ""
    action_text: str = ""


@dataclass
class RecordingSession:
    """The full parsed output of one PSR recording."""

    source_zip: Path
    steps: list[Step] = field(default_factory=list)
    title: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_zip(zip_path: Path, extract_to: Path) -> Path:
    """Validate and extract a PSR ZIP, returning the path of the inner .mht file.

    Raises ValueError if zip_path is not a valid ZIP or contains no .mht file.
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid ZIP file: {zip_path}")

    extract_to = Path(extract_to)
    extract_to.mkdir(parents=True, exist_ok=True)

    extract_to = extract_to.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        mht_names = [n for n in zf.namelist() if n.lower().endswith(".mht")]
        if not mht_names:
            raise ValueError(f"No .mht file found inside {zip_path.name}")

        # Guard against zip-slip: every member path must resolve inside extract_to.
        for member in zf.infolist():
            member_path = (extract_to / member.filename).resolve()
            try:
                member_path.relative_to(extract_to)
            except ValueError as exc:
                raise ValueError(
                    f"Unsafe ZIP entry detected (potential path traversal): {member.filename!r}"
                ) from exc

        zf.extractall(extract_to)

    return extract_to / mht_names[0]


def parse_mht(mht_path: Path, output_dir: Path) -> RecordingSession:
    """Parse a .mht file entirely in memory and return a populated RecordingSession.

    The MHT is read once into a BytesIO buffer; MIME walking and base64 decoding
    all operate on in-memory bytes. PNG files are written to disk in parallel via
    ThreadPoolExecutor to avoid sequential I/O bottlenecks.
    """
    mht_path = Path(mht_path)
    output_dir = Path(output_dir)
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Read the entire MHT into memory once.
    raw: bytes = read_mht_binary(mht_path)
    buf = io.BytesIO(raw)

    # Parse from raw bytes: more reliable than round-tripping through a string.
    msg = email.message_from_bytes(buf.read(), policy=email.policy.compat32)

    # Keep a decoded text copy for title extraction and metadata regex.
    mht_text = decode_mht_bytes(raw)

    image_parts: list[tuple[int, bytes, str]] = []
    html_text = ""

    for part in msg.walk():
        ctype = part.get_content_type()

        if ctype in ("image/png", "image/jpeg", "image/gif"):
            encoding = part.get("Content-Transfer-Encoding", "").lower()
            payload = part.get_payload()
            if not payload:
                continue
            if encoding == "base64":
                raw_b64 = _RE_WS.sub("", payload)
                try:
                    img_bytes = base64.b64decode(raw_b64)
                except Exception:
                    continue
            else:
                img_bytes = part.get_payload(decode=True) or b""

            location = part.get("Content-Location", "")
            image_parts.append((len(image_parts), img_bytes, location))

        elif ctype in ("text/html", "text/plain") and not html_text:
            payload = part.get_payload(decode=True)
            if payload:
                html_text = decode_mht_bytes(payload)
            elif isinstance(part.get_payload(), str):
                html_text = part.get_payload()

    # Write PNGs to disk in parallel; each task is independent I/O.
    def _write_png(args: tuple[int, bytes]) -> Path:
        seq, img_bytes = args
        png_path = screenshots_dir / f"step_{seq:03d}.png"
        png_path.write_bytes(img_bytes)
        return png_path

    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.map(_write_png, [(seq, img) for seq, img, _ in image_parts])

    # Build steps from in-memory metadata.
    step_meta = _extract_step_metadata(html_text)
    steps = _build_steps(image_parts, step_meta)

    title = _extract_title(mht_text)
    return RecordingSession(source_zip=mht_path, steps=steps, title=title)


def parse_session(zip_path: Path) -> RecordingSession:
    """Parse a PSR ZIP file and return a RecordingSession with all steps.

    The MHT inside the ZIP is read directly into memory via ZipFile.read() so
    no temporary directory is needed and no bytes hit the physical disk until
    the parallel PNG writer flushes them.
    """
    zip_path = safe_path(zip_path)
    output_dir = default_output_dir(zip_path)

    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid ZIP file: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        mht_names = [n for n in zf.namelist() if n.lower().endswith(".mht")]
        if not mht_names:
            raise ValueError(f"No .mht file found inside {zip_path.name}")

        # Read the MHT directly into memory without touching the filesystem.
        raw: bytes = zf.read(mht_names[0])

    session = _parse_mht_from_bytes(raw, output_dir)
    session.source_zip = zip_path
    return session


def generate_json_meta(session: RecordingSession, output_path: Path) -> Path:
    """Write structured JSON designed for LLM consumption.

    Schema includes title, source, generated_at, step_count, and per-step fields.
    Screenshot paths use forward slashes and are relative to output_path.parent.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_parent = output_path.parent

    steps_data = []
    for step in session.steps:
        screenshots_dir = out_parent / "screenshots"
        img_file = screenshots_dir / f"step_{step.index:03d}.png"
        if img_file.exists():
            try:
                rel = img_file.relative_to(out_parent).as_posix()
            except ValueError:
                rel = ""
        else:
            rel = ""

        steps_data.append({
            "index": step.index,
            "label": step.label,
            "timestamp": step.timestamp,
            "ui_element": step.ui_element,
            "program": step.program,
            "action_text": step.action_text,
            "screenshot": rel,
        })

    payload = {
        "title": session.title,
        "source": session.source_zip.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "step_count": len(session.steps),
        "steps": steps_data,
    }

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_path


def save_images(session: RecordingSession, out_dir: Path) -> list[Path]:
    """Write each step's image to out_dir and return the list of paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for step in session.steps:
        img_path = out_dir / f"step_{step.index:03d}.png"
        img_path.write_bytes(step.image_bytes)
        paths.append(img_path)
    return paths


# ---------------------------------------------------------------------------
# Internal: in-memory MHT parser (used by parse_session)
# ---------------------------------------------------------------------------

def _parse_mht_from_bytes(raw: bytes, output_dir: Path) -> RecordingSession:
    """Parse MHT bytes that are already in memory; same logic as parse_mht."""
    output_dir = Path(output_dir)
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO(raw)
    msg = email.message_from_bytes(buf.read(), policy=email.policy.compat32)
    mht_text = decode_mht_bytes(raw)

    image_parts: list[tuple[int, bytes, str]] = []
    html_text = ""

    for part in msg.walk():
        ctype = part.get_content_type()

        if ctype in ("image/png", "image/jpeg", "image/gif"):
            encoding = part.get("Content-Transfer-Encoding", "").lower()
            payload = part.get_payload()
            if not payload:
                continue
            if encoding == "base64":
                raw_b64 = _RE_WS.sub("", payload)
                try:
                    img_bytes = base64.b64decode(raw_b64)
                except Exception:
                    continue
            else:
                img_bytes = part.get_payload(decode=True) or b""

            location = part.get("Content-Location", "")
            image_parts.append((len(image_parts), img_bytes, location))

        elif ctype in ("text/html", "text/plain") and not html_text:
            payload = part.get_payload(decode=True)
            if payload:
                html_text = decode_mht_bytes(payload)
            elif isinstance(part.get_payload(), str):
                html_text = part.get_payload()

    def _write_png(args: tuple[int, bytes]) -> Path:
        seq, img_bytes = args
        png_path = screenshots_dir / f"step_{seq:03d}.png"
        png_path.write_bytes(img_bytes)
        return png_path

    with concurrent.futures.ThreadPoolExecutor() as executor:
        executor.map(_write_png, [(seq, img) for seq, img, _ in image_parts])

    step_meta = _extract_step_metadata(html_text)
    steps = _build_steps(image_parts, step_meta)

    title = _extract_title(mht_text)
    return RecordingSession(source_zip=output_dir, steps=steps, title=title)


# ---------------------------------------------------------------------------
# Internal: metadata extraction
# ---------------------------------------------------------------------------

def _extract_step_metadata_xml(html: str) -> list[dict]:
    """
    Extract per-step metadata from the PSR embedded XML block.

    PSR embeds recording data as:
        <script id="myXML" type="text/xml">
          <?xml version="1.0" encoding="UTF-8"?>
          <Report>
            <UserActionData>
              <RecordSession ...>
                <EachAction ActionNumber="1" Time="17:59:58" FileDescription="Google Chrome" ...>
                  <Description>User left-clicked on ...</Description>
                  <Action>Left click</Action>
                  <UIAStack>
                    <Level Name="Button name" ... />
                  </UIAStack>
                  <ScreenshotFileName>screenshot0001.JPEG</ScreenshotFileName>
                </EachAction>
              </RecordSession>
            </UserActionData>
          </Report>
        </script>

    Returns a list of dicts keyed by 0-based screenshot index.
    """
    # Extract the XML block from inside the <script type="text/xml"> tag
    xml_match = re.search(
        r'<script[^>]+type=["\']text/xml["\'][^>]*>\s*(.*?)\s*</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not xml_match:
        return []

    xml_text = xml_match.group(1).strip()
    # Strip the <?xml ...?> declaration if present (ElementTree handles it better without)
    xml_text = re.sub(r'<\?xml[^?]*\?>', '', xml_text, count=1).strip()

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    actions = root.findall(".//EachAction")
    results: list[dict] = []

    for action in actions:
        # Program name from FileDescription attribute on the EachAction tag
        program = action.get("FileDescription", "")
        time_str = action.get("Time", "")

        # Human-readable description (localized, e.g. Hebrew)
        desc_el = action.find("Description")
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        # Action type (e.g. "Left click", "Scroll")
        action_el = action.find("Action")
        action_type = action_el.text.strip() if action_el is not None and action_el.text else ""

        # UI element name: deepest Level with a Name attribute in UIAStack
        ui_name = ""
        uia_stack = action.find("UIAStack")
        if uia_stack is not None:
            levels = uia_stack.findall("Level")
            for level in levels:
                name = level.get("Name", "").strip()
                if name:
                    ui_name = name  # last non-empty Name = most specific element

        # Screenshot filename (e.g. "screenshot0001.JPEG")
        screen_el = action.find("ScreenshotFileName")
        screenshot = screen_el.text.strip() if screen_el is not None and screen_el.text else ""

        # Build a clean label: "Action in UI element"
        if ui_name and action_type:
            label = f"{action_type} on '{ui_name}'"
        elif description:
            label = description[:120]
        else:
            label = action_type or f"Action {action.get('ActionNumber', '?')}"

        results.append({
            "label": label,
            "timestamp": time_str,
            "ui_element": ui_name,
            "program": program,
            "action_text": description,
            "screenshot_filename": screenshot,
        })

    return results


def _extract_step_metadata(html: str) -> list[dict]:
    """Extract per-step metadata from the PSR HTML/XML structure.

    Tries the embedded XML block first (all modern PSR versions).
    Falls back to HTML regex patterns for older PSR versions.
    Returns a list of dicts, one per detected step.
    """
    if not html:
        return []

    # Primary path: parse the embedded XML block
    xml_results = _extract_step_metadata_xml(html)
    if xml_results:
        return xml_results

    # Fallback: HTML regex patterns (older PSR or non-standard formats)
    results: list[dict] = []

    for m in _RE_STEP.finditer(html):
        label_text = (m.group(2) or "").strip()
        ts_m = _RE_TIMESTAMP.search(html, m.start())
        ts = ts_m.group(1).strip() if ts_m else ""
        prog_m = _RE_PROGRAM.search(html, m.start())
        prog = prog_m.group(1).strip() if prog_m else ""
        results.append({
            "label": label_text,
            "timestamp": ts,
            "ui_element": "",
            "program": prog,
            "action_text": label_text,
            "screenshot_filename": "",
        })

    if not results:
        for i, m in enumerate(_RE_DD.finditer(html)):
            inner = _RE_TAGS.sub("", m.group(1)).strip()
            results.append({
                "label": inner[:80] if inner else f"Step {i}",
                "timestamp": "",
                "ui_element": "",
                "program": "",
                "action_text": inner,
                "screenshot_filename": "",
            })

    return results


def _build_steps(
    image_parts: list[tuple[int, bytes, str]],
    step_meta: list[dict],
) -> list[Step]:
    """
    Match image parts to metadata records and return a list of Step objects.

    When XML metadata is present, each record has a 'screenshot_filename' field
    (e.g. 'screenshot0001.JPEG'). We build a filename->meta lookup so images are
    matched by name rather than position - this is robust against gaps in the
    action list (PSR sometimes skips screenshots for intermediate events).

    Falls back to positional matching when filenames are unavailable.
    """
    # Build filename lookup from metadata (lowercase, stem-only for tolerance)
    filename_to_meta: dict[str, dict] = {}
    for meta in step_meta:
        fname = meta.get("screenshot_filename", "")
        if fname:
            # Normalize: lowercase stem without extension
            stem = Path(fname).stem.lower()
            filename_to_meta[stem] = meta

    steps: list[Step] = []
    for seq, img_bytes, location in image_parts:
        # Try to match by filename stem derived from MIME Content-Location
        meta: dict = {}
        if filename_to_meta:
            loc_stem = Path(location).stem.lower() if location else ""
            if loc_stem in filename_to_meta:
                meta = filename_to_meta[loc_stem]
        # Positional fallback
        if not meta and seq < len(step_meta):
            meta = step_meta[seq]

        label = meta.get("label") or _label_from_location(location) or f"Step {seq}"
        steps.append(Step(
            index=seq,
            label=label,
            image_bytes=img_bytes,
            timestamp=meta.get("timestamp", ""),
            ui_element=meta.get("ui_element", ""),
            program=meta.get("program", ""),
            action_text=meta.get("action_text", ""),
        ))

    return steps


def _label_from_location(location: str) -> str:
    """Derive a human-readable step label from the MIME Content-Location header."""
    if not location:
        return "Step"
    stem = Path(location).stem
    return stem.replace("_", " ").replace("-", " ").title()


def _extract_title(mht_text: str) -> str:
    """Pull the recording title from the MHT HTML content if present."""
    match = _RE_TITLE.search(mht_text)
    return match.group(1).strip() if match else "PSR Recording"
