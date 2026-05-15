"""Unit tests for utils/paths.py - safe_path and sanitize_filename."""

import pytest
from pathlib import Path
from psr_ai.utils.paths import sanitize_filename, default_output_dir, safe_path


def test_sanitize_removes_illegal_windows_chars():
    # Trailing illegal char becomes "_", then strip("_") removes it from the edge.
    assert sanitize_filename("step:01?output*") == "step_01_output"


def test_sanitize_collapses_underscores():
    # Multiple consecutive replacements collapse to a single underscore.
    assert sanitize_filename("a::b") == "a_b"


def test_sanitize_returns_unnamed_for_empty():
    # A string made entirely of illegal characters should yield "unnamed".
    assert sanitize_filename(":::") == "unnamed"


def test_sanitize_clean_name_unchanged():
    assert sanitize_filename("session_2024_01_01") == "session_2024_01_01"


def test_default_output_dir_with_source(tmp_path):
    src = tmp_path / "recording.zip"
    out = default_output_dir(src)
    assert out == tmp_path / "recording_output"


def test_default_output_dir_no_source():
    out = default_output_dir()
    assert out.name == "psr_output"


def test_safe_path_within_base(tmp_path):
    target = tmp_path / "subdir" / "file.txt"
    result = safe_path(target, base=tmp_path)
    assert result == target.resolve()


def test_safe_path_traversal_rejected(tmp_path):
    evil = tmp_path / ".." / "escape.txt"
    with pytest.raises(ValueError, match="outside the allowed base directory"):
        safe_path(evil, base=tmp_path)
