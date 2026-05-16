"""Unit tests for core/parser.py - uses synthetic MHT/ZIP data, no real PSR output needed."""

from __future__ import annotations

import base64
import json
import random
import zipfile
from pathlib import Path

import pytest

from psr_ai.core.parser import (
    RecordingSession,
    Step,
    extract_zip,
    generate_json_meta,
    parse_mht,
    parse_session,
    save_images,
)


# ---------------------------------------------------------------------------
# Minimal 1x1 transparent PNG (valid image bytes for testing)
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Helper: build a synthetic MHT string with embedded PNG images
# ---------------------------------------------------------------------------

def _make_mht_content(num_images: int = 2, title: str = "Test Recording") -> str:
    """Build a minimal MIME multipart MHT string with num_images embedded PNGs."""
    boundary = "----=_NextPart_000_TEST"
    parts: list[str] = []

    # Outer header
    header = (
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: multipart/related; boundary=\"{boundary}\"\r\n"
        f"Subject: {title}\r\n"
        f"\r\n"
    )

    # HTML part with step markup
    step_html_parts = []
    for i in range(num_images):
        step_html_parts.append(
            f'<p class="step">Step {i}: <b>Click button {i}</b></p>'
        )
    html_body = (
        f"<html><head><title>{title}</title></head><body>"
        + "".join(step_html_parts)
        + "</body></html>"
    )
    html_part = (
        f"--{boundary}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: quoted-printable\r\n"
        f"\r\n"
        f"{html_body}\r\n"
    )
    parts.append(html_part)

    # Image parts
    for i in range(num_images):
        b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        # Insert linebreaks every 76 chars as PSR does.
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
    return header + "".join(parts) + terminator


def _write_mht(path: Path, content: str) -> None:
    """Write MHT content as raw bytes - preserves CRLF on Windows.

    write_text() on Windows translates \\n to \\r\\n, turning \\r\\n into \\r\\r\\n
    which corrupts MIME boundary detection. Always write as encoded bytes.
    """
    path.write_bytes(content.encode("utf-8"))


def make_test_zip(tmp_path: Path, mht_content: str) -> Path:
    """Create a valid ZIP containing a single .mht file and return the zip path."""
    mht_path = tmp_path / "recording.mht"
    _write_mht(mht_path, mht_content)

    zip_path = tmp_path / "recording.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(mht_path, arcname="recording.mht")

    return zip_path


# ---------------------------------------------------------------------------
# extract_zip tests
# ---------------------------------------------------------------------------

def test_extract_zip_valid(tmp_path):
    mht_content = _make_mht_content(num_images=1)
    zip_path = make_test_zip(tmp_path, mht_content)
    extract_dir = tmp_path / "extracted"

    mht_result = extract_zip(zip_path, extract_dir)

    assert mht_result.exists()
    assert mht_result.suffix.lower() == ".mht"
    assert mht_result.parent == extract_dir


def test_extract_zip_invalid_file(tmp_path):
    not_a_zip = tmp_path / "fake.zip"
    not_a_zip.write_bytes(b"this is not a zip file at all")

    with pytest.raises(ValueError, match="Not a valid ZIP"):
        extract_zip(not_a_zip, tmp_path / "out")


def test_extract_zip_missing_mht(tmp_path):
    # Build a zip with a .txt file instead of .mht.
    txt_file = tmp_path / "readme.txt"
    txt_file.write_text("hello")
    zip_path = tmp_path / "no_mht.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(txt_file, arcname="readme.txt")

    with pytest.raises(ValueError, match="No .mht file"):
        extract_zip(zip_path, tmp_path / "out")


# ---------------------------------------------------------------------------
# parse_mht tests
# ---------------------------------------------------------------------------

def test_parse_mht_extracts_images(tmp_path):
    mht_content = _make_mht_content(num_images=2)
    mht_file = tmp_path / "recording.mht"
    _write_mht(mht_file, mht_content)
    output_dir = tmp_path / "output"

    session = parse_mht(mht_file, output_dir)

    screenshots_dir = output_dir / "screenshots"
    assert screenshots_dir.exists()
    saved = list(screenshots_dir.glob("step_*.png"))
    assert len(saved) == 2


def test_parse_mht_step_count(tmp_path):
    mht_content = _make_mht_content(num_images=3)
    mht_file = tmp_path / "recording.mht"
    _write_mht(mht_file, mht_content)
    output_dir = tmp_path / "output"

    session = parse_mht(mht_file, output_dir)

    assert len(session.steps) == 3


def test_parse_mht_step_has_image_bytes(tmp_path):
    mht_content = _make_mht_content(num_images=1)
    mht_file = tmp_path / "recording.mht"
    _write_mht(mht_file, mht_content)
    output_dir = tmp_path / "output"

    session = parse_mht(mht_file, output_dir)

    assert len(session.steps) == 1
    assert len(session.steps[0].image_bytes) > 0
    # Must be PNG magic bytes.
    assert session.steps[0].image_bytes[:4] == b"\x89PNG"


def test_parse_mht_title_extracted(tmp_path):
    mht_content = _make_mht_content(num_images=1, title="My Test Title")
    mht_file = tmp_path / "recording.mht"
    _write_mht(mht_file, mht_content)
    output_dir = tmp_path / "output"

    session = parse_mht(mht_file, output_dir)

    assert session.title == "My Test Title"


# ---------------------------------------------------------------------------
# parse_session end-to-end test
# ---------------------------------------------------------------------------

def test_parse_session_end_to_end(tmp_path):
    mht_content = _make_mht_content(num_images=2)
    zip_path = make_test_zip(tmp_path, mht_content)

    session = parse_session(zip_path)

    assert isinstance(session, RecordingSession)
    assert session.source_zip == zip_path.resolve()
    assert len(session.steps) == 2
    for step in session.steps:
        assert isinstance(step, Step)
        assert len(step.image_bytes) > 0


# ---------------------------------------------------------------------------
# generate_json_meta tests
# ---------------------------------------------------------------------------

def test_generate_json_meta_schema(tmp_path):
    mht_content = _make_mht_content(num_images=2)
    zip_path = make_test_zip(tmp_path, mht_content)
    session = parse_session(zip_path)

    output_dir = tmp_path / "meta_output"
    output_dir.mkdir()
    json_path = generate_json_meta(session, output_dir / "data.json")

    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Required top-level fields.
    assert "title" in data
    assert "source" in data
    assert "generated_at" in data
    assert "step_count" in data
    assert "steps" in data
    assert data["step_count"] == len(session.steps)

    # Required per-step fields.
    for step_entry in data["steps"]:
        for field in ("index", "label", "timestamp", "ui_element", "program", "action_text", "screenshot"):
            assert field in step_entry, f"Missing field: {field}"


def test_generate_json_meta_no_colons_in_paths(tmp_path):
    mht_content = _make_mht_content(num_images=1)
    zip_path = make_test_zip(tmp_path, mht_content)
    session = parse_session(zip_path)

    output_dir = tmp_path / "no_colon_output"
    output_dir.mkdir()
    json_path = generate_json_meta(session, output_dir / "data.json")
    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Screenshot paths must use forward slashes, not backslashes.
    for step_entry in data["steps"]:
        screenshot = step_entry["screenshot"]
        if screenshot:
            assert "\\" not in screenshot, f"Backslash in screenshot path: {screenshot}"


def test_generate_json_meta_step_count_zero(tmp_path):
    # Edge case: session with no steps.
    session = RecordingSession(source_zip=tmp_path / "empty.zip", steps=[], title="Empty")
    output_dir = tmp_path / "empty_out"
    output_dir.mkdir()
    json_path = generate_json_meta(session, output_dir / "data.json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["step_count"] == 0
    assert data["steps"] == []


# ---------------------------------------------------------------------------
# decode_never_crashes stress test
# ---------------------------------------------------------------------------

def test_decode_never_crashes(tmp_path):
    """Feed 1000 random byte sequences through decode_mht_bytes - must never raise."""
    from psr_ai.utils.encoding import decode_mht_bytes

    rng = random.Random(42)
    for _ in range(1000):
        length = rng.randint(0, 512)
        data = bytes(rng.randint(0, 255) for _ in range(length))
        # Must not raise under any circumstances.
        result = decode_mht_bytes(data)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# save_images
# ---------------------------------------------------------------------------

def test_save_images_creates_files(tmp_path):
    steps = [
        Step(index=0, label="Step 0", image_bytes=_TINY_PNG),
        Step(index=1, label="Step 1", image_bytes=_TINY_PNG),
    ]
    session = RecordingSession(source_zip=tmp_path / "fake.zip", steps=steps, title="Test")
    out_dir = tmp_path / "imgs"

    paths = save_images(session, out_dir)

    assert len(paths) == 2
    for p in paths:
        assert p.exists()
        assert p.read_bytes() == _TINY_PNG
