"""Parses PSR output: auto-unzips the archive, reads the MHT, extracts PNG steps."""

from __future__ import annotations

import base64
import email
import email.policy
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from psr_ai.utils.encoding import decode_mht_bytes, read_mht_binary
from psr_ai.utils.paths import safe_path, default_output_dir


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


def extract_zip(zip_path: Path, extract_to: Path) -> Path:
    """Validate and extract a PSR ZIP, returning the path of the inner .mht file.

    Raises ValueError if zip_path is not a valid ZIP or contains no .mht file.
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid ZIP file: {zip_path}")

    extract_to = Path(extract_to)
    extract_to.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_to)
        mht_names = [n for n in zf.namelist() if n.lower().endswith(".mht")]

    if not mht_names:
        raise ValueError(f"No .mht file found inside {zip_path.name}")

    # PSR always produces exactly one MHT; take the first match.
    return extract_to / mht_names[0]


def parse_mht(mht_path: Path, output_dir: Path) -> RecordingSession:
    """Parse a .mht file and return a populated RecordingSession.

    Uses email.message_from_bytes on the raw bytes so the MIME parser handles
    boundary detection itself, avoiding charset round-trip issues (Pitfall 1).
    """
    mht_path = Path(mht_path)
    output_dir = Path(output_dir)
    screenshots_dir = output_dir / "screenshots"

    raw = read_mht_binary(mht_path)

    # Parse from raw bytes: more reliable than round-tripping through a string.
    # email.policy.compat32 preserves the original MIME structure PSR produces.
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)

    # Also keep a decoded text copy for title extraction and metadata regex.
    mht_text = decode_mht_bytes(raw)

    image_parts: list[tuple[int, bytes, str]] = []  # (sequence_index, bytes, location)
    html_text = ""

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("image/png", "image/jpeg", "image/gif"):
            encoding = part.get("Content-Transfer-Encoding", "").lower()
            payload = part.get_payload()
            if not payload:
                continue
            if encoding == "base64":
                # Strip whitespace that some PSR versions insert in long base64 lines.
                raw_b64 = re.sub(r"\s+", "", payload)
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

    # Save screenshots and build steps.
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    step_meta = _extract_step_metadata(html_text)
    steps: list[Step] = []

    for seq, img_bytes, location in image_parts:
        img_path = screenshots_dir / f"step_{seq:03d}.png"
        img_path.write_bytes(img_bytes)

        meta = step_meta[seq] if seq < len(step_meta) else {}
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

    title = _extract_title(mht_text)
    session = RecordingSession(
        source_zip=mht_path,  # caller will override with zip_path
        steps=steps,
        title=title,
    )
    return session


def _extract_step_metadata(html: str) -> list[dict]:
    """Extract per-step metadata from the PSR HTML structure.

    PSR uses patterns like:
      <p class="step">Step N: <b>Action text</b></p>
      <dd>...</dd> blocks with timestamps and UI info.
    Returns a list of dicts, one per detected step (may be shorter than image count).
    """
    if not html:
        return []

    results: list[dict] = []

    # Primary pattern: PSR step headings.
    step_pattern = re.compile(
        r'<[^>]*class=["\']?step["\']?[^>]*>\s*'
        r'(?:Step\s*(\d+)\s*[:\-])?\s*'
        r'(?:<b[^>]*>([^<]*)</b>)?',
        re.IGNORECASE,
    )
    # Timestamp pattern inside PSR HTML.
    timestamp_pattern = re.compile(
        r'(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?)',
        re.IGNORECASE,
    )
    # Program/window title pattern.
    program_pattern = re.compile(
        r'(?:window title|program):\s*([^\n<]+)',
        re.IGNORECASE,
    )

    for m in step_pattern.finditer(html):
        label_text = (m.group(2) or "").strip()
        ts_m = timestamp_pattern.search(html, m.start())
        ts = ts_m.group(1).strip() if ts_m else ""
        prog_m = program_pattern.search(html, m.start())
        prog = prog_m.group(1).strip() if prog_m else ""
        results.append({
            "label": label_text,
            "timestamp": ts,
            "ui_element": "",
            "program": prog,
            "action_text": label_text,
        })

    # Fallback: look for <dd> blocks used in some PSR versions.
    if not results:
        dd_pattern = re.compile(r'<dd[^>]*>(.*?)</dd>', re.IGNORECASE | re.DOTALL)
        for i, m in enumerate(dd_pattern.finditer(html)):
            inner = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            results.append({
                "label": inner[:80] if inner else f"Step {i}",
                "timestamp": "",
                "ui_element": "",
                "program": "",
                "action_text": inner,
            })

    return results


def parse_session(zip_path: Path) -> RecordingSession:
    """Parse a PSR ZIP file and return a RecordingSession with all steps.

    PSR always wraps the MHT inside a ZIP (Pitfall 4).
    The MHT may lack a charset declaration (Pitfall 1): decode_mht_bytes handles that.
    """
    zip_path = safe_path(zip_path)
    output_dir = default_output_dir(zip_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mht_path = extract_zip(zip_path, tmp_path)
        session = parse_mht(mht_path, output_dir)

    # Fix source_zip to point to the original zip, not the tmp mht.
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
        # Compute relative screenshot path using forward slashes.
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


def _label_from_location(location: str) -> str:
    """Derive a human-readable step label from the MIME Content-Location header."""
    if not location:
        return "Step"
    stem = Path(location).stem
    # Replace underscores and hyphens with spaces for readability.
    return stem.replace("_", " ").replace("-", " ").title()


def _extract_title(mht_text: str) -> str:
    """Pull the recording title from the MHT HTML content if present."""
    match = re.search(r"<title[^>]*>([^<]+)</title>", mht_text, re.IGNORECASE)
    return match.group(1).strip() if match else "PSR Recording"
