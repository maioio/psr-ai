"""Tests for the in-memory parsing path added in the performance overhaul.

Verifies that parse_session reads the MHT directly from the ZIP into memory
(no tempdir) and that compiled regex patterns produce identical results to
the original string-based approach.
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from psr_ai.core.parser import (
    RecordingSession,
    Step,
    _extract_step_metadata,
    _extract_title,
    _label_from_location,
    _parse_mht_from_bytes,
    parse_session,
    _RE_STEP,
    _RE_TIMESTAMP,
    _RE_PROGRAM,
)


# ---------------------------------------------------------------------------
# Minimal PNG bytes (1x1 transparent)
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# MHT / ZIP builders
# ---------------------------------------------------------------------------

def _make_mht_bytes(num_images: int = 2, title: str = "In-Memory Test") -> bytes:
    """Return a minimal MIME multipart MHT as raw bytes."""
    boundary = "----=_NextPart_000_MEM"
    parts: list[str] = []

    header = (
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: multipart/related; boundary=\"{boundary}\"\r\n"
        f"Subject: {title}\r\n"
        f"\r\n"
    )

    step_html = "".join(
        f'<p class="step">Step {i}: <b>Clicked item {i}</b></p>'
        for i in range(num_images)
    )
    html_body = f"<html><head><title>{title}</title></head><body>{step_html}</body></html>"
    html_part = (
        f"--{boundary}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: quoted-printable\r\n"
        f"\r\n"
        f"{html_body}\r\n"
    )
    parts.append(html_part)

    for i in range(num_images):
        b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        b64_lines = "\r\n".join(b64[j:j+76] for j in range(0, len(b64), 76))
        img_part = (
            f"--{boundary}\r\n"
            f"Content-Type: image/png\r\n"
            f"Content-Transfer-Encoding: base64\r\n"
            f"Content-Location: step_{i:03d}.png\r\n"
            f"\r\n"
            f"{b64_lines}\r\n"
        )
        parts.append(img_part)

    terminator = f"--{boundary}--\r\n"
    full = header + "".join(parts) + terminator
    return full.encode("utf-8")


def _make_zip_bytes(num_images: int = 2, title: str = "In-Memory Test") -> bytes:
    """Return a PSR-style ZIP (containing one .mht) as bytes in memory."""
    import io
    mht_bytes = _make_mht_bytes(num_images=num_images, title=title)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("recording.mht", mht_bytes)
    return buf.getvalue()


def _write_zip(tmp_path: Path, num_images: int = 2, title: str = "In-Memory Test") -> Path:
    """Write a PSR-style ZIP to disk and return its path."""
    zip_path = tmp_path / "recording.zip"
    zip_path.write_bytes(_make_zip_bytes(num_images=num_images, title=title))
    return zip_path


# ---------------------------------------------------------------------------
# Compiled regex pattern tests
# ---------------------------------------------------------------------------

class TestCompiledPatterns:

    def test_re_step_matches_psr_heading(self):
        html = '<p class="step">Step 3: <b>Clicked Save button</b></p>'
        matches = list(_RE_STEP.finditer(html))
        assert len(matches) == 1
        assert matches[0].group(2) == "Clicked Save button"

    def test_re_step_case_insensitive(self):
        html = '<P CLASS="STEP">Step 0: <B>Open file</B></P>'
        assert _RE_STEP.search(html) is not None

    def test_re_timestamp_parses_psr_format(self):
        html = "Recorded at 1/15/2024 2:30:00 PM in Chrome"
        m = _RE_TIMESTAMP.search(html)
        assert m is not None
        assert "2024" in m.group(1)

    def test_re_program_extracts_window_title(self):
        html = "Window title: Visual Studio Code"
        m = _RE_PROGRAM.search(html)
        assert m is not None
        assert "Visual Studio Code" in m.group(1)

    def test_re_step_no_match_on_non_step_html(self):
        html = "<p class='other'>Normal paragraph</p>"
        assert _RE_STEP.search(html) is None


# ---------------------------------------------------------------------------
# _parse_mht_from_bytes (in-memory path)
# ---------------------------------------------------------------------------

class TestParseMhtFromBytes:

    def test_extracts_correct_step_count(self, tmp_path):
        raw = _make_mht_bytes(num_images=3)
        session = _parse_mht_from_bytes(raw, tmp_path)
        assert len(session.steps) == 3

    def test_extracts_title(self, tmp_path):
        raw = _make_mht_bytes(title="Custom Session Title")
        session = _parse_mht_from_bytes(raw, tmp_path)
        assert session.title == "Custom Session Title"

    def test_steps_have_image_bytes(self, tmp_path):
        raw = _make_mht_bytes(num_images=2)
        session = _parse_mht_from_bytes(raw, tmp_path)
        for step in session.steps:
            assert len(step.image_bytes) > 0
            assert step.image_bytes[:4] == b"\x89PNG"

    def test_png_files_written_to_screenshots_dir(self, tmp_path):
        raw = _make_mht_bytes(num_images=2)
        _parse_mht_from_bytes(raw, tmp_path)
        for i in range(2):
            assert (tmp_path / "screenshots" / f"step_{i:03d}.png").exists()

    def test_empty_mht_returns_empty_session(self, tmp_path):
        boundary = "----=_EMPTY"
        empty_mht = (
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/related; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        session = _parse_mht_from_bytes(empty_mht, tmp_path)
        assert len(session.steps) == 0

    def test_parallel_write_produces_all_files(self, tmp_path):
        """ThreadPoolExecutor must not silently drop any frames."""
        raw = _make_mht_bytes(num_images=10)
        _parse_mht_from_bytes(raw, tmp_path)
        written = list((tmp_path / "screenshots").glob("step_*.png"))
        assert len(written) == 10


# ---------------------------------------------------------------------------
# parse_session: in-memory ZIP read (no tempdir)
# ---------------------------------------------------------------------------

class TestParseSessionInMemory:

    def test_reads_without_temp_directory(self, tmp_path):
        """parse_session must not import or use tempfile (in-memory path)."""
        import psr_ai.core.parser as _parser_mod
        # tempfile was removed from the in-memory parser; it must not be present.
        assert not hasattr(_parser_mod, "tempfile"), (
            "parser.py still imports tempfile - the in-memory refactor is incomplete."
        )
        # The call must succeed cleanly without disk-based temp extraction.
        zip_path = _write_zip(tmp_path, num_images=2)
        session = parse_session(zip_path)
        assert len(session.steps) == 2

    def test_session_has_correct_step_count(self, tmp_path):
        zip_path = _write_zip(tmp_path, num_images=3)
        session = parse_session(zip_path)
        assert len(session.steps) == 3

    def test_source_zip_is_set_correctly(self, tmp_path):
        zip_path = _write_zip(tmp_path, num_images=1)
        session = parse_session(zip_path)
        assert session.source_zip == zip_path.resolve()

    def test_title_extracted_from_zip(self, tmp_path):
        zip_path = _write_zip(tmp_path, title="ZIP Session Title")
        session = parse_session(zip_path)
        assert session.title == "ZIP Session Title"

    def test_raises_on_invalid_zip(self, tmp_path):
        bad_zip = tmp_path / "not_a_zip.zip"
        bad_zip.write_bytes(b"definitely not a zip")
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            parse_session(bad_zip)

    def test_raises_when_no_mht_in_zip(self, tmp_path):
        import io as _io
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "no mht here")
        zip_path = tmp_path / "no_mht.zip"
        zip_path.write_bytes(buf.getvalue())
        with pytest.raises(ValueError, match="No .mht file"):
            parse_session(zip_path)


# ---------------------------------------------------------------------------
# _extract_step_metadata: compiled regex consistency
# ---------------------------------------------------------------------------

class TestExtractStepMetadata:

    def test_extracts_labels_from_step_headings(self):
        html = (
            '<p class="step">Step 0: <b>Open document</b></p>'
            '<p class="step">Step 1: <b>Save file</b></p>'
        )
        results = _extract_step_metadata(html)
        assert len(results) == 2
        assert results[0]["label"] == "Open document"
        assert results[1]["label"] == "Save file"

    def test_falls_back_to_dd_blocks(self):
        html = "<dd>First action</dd><dd>Second action</dd>"
        results = _extract_step_metadata(html)
        assert len(results) == 2
        assert results[0]["action_text"] == "First action"

    def test_empty_html_returns_empty_list(self):
        assert _extract_step_metadata("") == []

    def test_action_text_matches_label(self):
        html = '<p class="step">Step 0: <b>Click the button</b></p>'
        results = _extract_step_metadata(html)
        assert results[0]["action_text"] == results[0]["label"]


# ---------------------------------------------------------------------------
# _extract_title and _label_from_location
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_extract_title_from_html(self):
        html = "<html><head><title>My Recording</title></head></html>"
        assert _extract_title(html) == "My Recording"

    def test_extract_title_fallback(self):
        assert _extract_title("<html><body>no title</body></html>") == "PSR Recording"

    def test_label_from_location_empty(self):
        assert _label_from_location("") == "Step"

    def test_label_from_location_formats_filename(self):
        label = _label_from_location("step_003.png")
        assert label == "Step 003"
