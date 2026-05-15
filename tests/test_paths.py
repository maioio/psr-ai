"""Unit tests for utils/paths.py - sanitize_filename, timestamp_filename, safe_path."""

import re
import pytest
from pathlib import Path

from psr_ai.utils.paths import (
    sanitize_filename,
    timestamp_filename,
    safe_path,
    resolve_env_path,
    default_output_dir,
)


# ------------------------------------------------------------------
# sanitize_filename
# ------------------------------------------------------------------

def test_sanitize_removes_colons_from_timestamp():
    # Colons are the most common offender - they appear in ISO timestamps.
    assert sanitize_filename("psr_2024-01-15T14:30:00") == "psr_2024-01-15T14_30_00"


def test_sanitize_removes_all_illegal_windows_chars():
    # Trailing illegal char becomes "_", then strip("_") removes it from the edge.
    assert sanitize_filename("step:01?output*") == "step_01_output"


def test_sanitize_collapses_consecutive_replacements():
    # Multiple consecutive illegal chars collapse to a single underscore.
    assert sanitize_filename("a::b") == "a_b"


def test_sanitize_returns_unnamed_for_all_illegal():
    assert sanitize_filename(":::") == "unnamed"


def test_sanitize_returns_unnamed_for_empty():
    assert sanitize_filename("") == "unnamed"


def test_sanitize_clean_name_unchanged():
    assert sanitize_filename("session_2024_01_01") == "session_2024_01_01"


def test_sanitize_rejects_windows_reserved_name():
    assert sanitize_filename("CON") == "unnamed"
    assert sanitize_filename("NUL") == "unnamed"
    assert sanitize_filename("COM1") == "unnamed"


def test_sanitize_strips_trailing_dots():
    # Windows disallows trailing dots on directory/file names.
    assert not sanitize_filename("name.").endswith(".")


# ------------------------------------------------------------------
# timestamp_filename
# ------------------------------------------------------------------

def test_timestamp_filename_format():
    name = timestamp_filename("psr", ".zip")
    # Must match: psr_YYYYMMDD_HHMMSS.zip
    assert re.match(r"^psr_\d{8}_\d{6}\.zip$", name), f"Unexpected format: {name}"


def test_timestamp_filename_no_colons():
    name = timestamp_filename("rec", ".zip")
    assert ":" not in name


def test_timestamp_filename_custom_prefix():
    name = timestamp_filename("session", ".mht")
    assert name.startswith("session_")
    assert name.endswith(".mht")


# ------------------------------------------------------------------
# safe_path
# ------------------------------------------------------------------

def test_safe_path_within_base(tmp_path):
    target = tmp_path / "subdir" / "file.txt"
    result = safe_path(target, base=tmp_path)
    assert result == target.resolve()


def test_safe_path_traversal_rejected(tmp_path):
    evil = tmp_path / ".." / "escape.txt"
    with pytest.raises(ValueError, match="outside the allowed base directory"):
        safe_path(evil, base=tmp_path)


def test_safe_path_no_base_does_not_raise(tmp_path):
    result = safe_path(tmp_path / "anywhere.txt")
    assert isinstance(result, Path)


# ------------------------------------------------------------------
# resolve_env_path
# ------------------------------------------------------------------

def test_resolve_env_path_systemroot():
    # SystemRoot is always set on Windows.
    p = resolve_env_path("SystemRoot", "System32")
    assert p.parts[-1] == "System32"


def test_resolve_env_path_missing_var():
    with pytest.raises(KeyError):
        resolve_env_path("THIS_VAR_DOES_NOT_EXIST_XYZ")


# ------------------------------------------------------------------
# default_output_dir
# ------------------------------------------------------------------

def test_default_output_dir_with_source(tmp_path):
    src = tmp_path / "recording.zip"
    out = default_output_dir(src)
    assert out == tmp_path / "recording_output"


def test_default_output_dir_no_source():
    out = default_output_dir()
    assert out.name == "psr_output"
