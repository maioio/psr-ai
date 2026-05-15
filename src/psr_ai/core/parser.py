"""Parses PSR output: auto-unzips the archive, reads the MHT, extracts PNG steps."""

from __future__ import annotations

import base64
import email
import email.policy
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from psr_ai.utils.encoding import decode_mht_bytes
from psr_ai.utils.paths import safe_path


@dataclass
class Step:
    """A single recorded step: screenshot bytes and descriptive text."""

    index: int
    label: str
    image_bytes: bytes
    timestamp: str = ""


@dataclass
class RecordingSession:
    """The full parsed output of one PSR recording."""

    source_zip: Path
    steps: list[Step] = field(default_factory=list)
    title: str = ""


def parse_session(zip_path: Path) -> RecordingSession:
    """Parse a PSR ZIP file and return a RecordingSession with all steps.

    PSR always wraps the MHT inside a ZIP (Pitfall 4).
    The MHT may lack a charset declaration (Pitfall 1) - decode_mht_bytes handles that.
    """
    zip_path = safe_path(zip_path)
    session = RecordingSession(source_zip=zip_path)

    mht_bytes = _extract_mht_bytes(zip_path)
    mht_text = decode_mht_bytes(mht_bytes)
    session.steps = _parse_mht(mht_text)
    session.title = _extract_title(mht_text)
    return session


def _extract_mht_bytes(zip_path: Path) -> bytes:
    """Open the ZIP and return the raw bytes of the inner .mht file."""
    with zipfile.ZipFile(zip_path) as zf:
        mht_names = [n for n in zf.namelist() if n.lower().endswith(".mht")]
        if not mht_names:
            raise ValueError(f"No .mht file found inside {zip_path.name}")
        # PSR always produces exactly one MHT; take the first match.
        return zf.read(mht_names[0])


def _parse_mht(mht_text: str) -> list[Step]:
    """Extract steps from the decoded MHT text using Python's email MIME parser."""
    # Use email.message_from_string for MIME multipart boundary handling.
    msg = email.message_from_string(mht_text, policy=email.policy.compat32)
    steps: list[Step] = []
    index = 0

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("image/png", "image/jpeg", "image/gif"):
            continue
        encoding = part.get("Content-Transfer-Encoding", "").lower()
        payload = part.get_payload()
        if not payload:
            continue

        if encoding == "base64":
            # Strip whitespace that some PSR versions insert in long base64 lines.
            raw = re.sub(r"\s+", "", payload)
            try:
                img_bytes = base64.b64decode(raw)
            except Exception:
                continue
        else:
            img_bytes = part.get_payload(decode=True) or b""

        location = part.get("Content-Location", "")
        steps.append(Step(index=index, label=_label_from_location(location), image_bytes=img_bytes))
        index += 1

    return steps


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


def save_images(session: RecordingSession, out_dir: Path) -> list[Path]:
    """Write each step's image to out_dir and return the list of paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for step in session.steps:
        img_path = out_dir / f"step_{step.index:03d}.png"
        img_path.write_bytes(step.image_bytes)
        paths.append(img_path)
    return paths
