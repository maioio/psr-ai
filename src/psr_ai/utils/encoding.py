"""Charset detection for MHT files that lack or lie about their encoding.

PSR produces MHT files with inconsistent or absent charset declarations (Pitfall 1).
Always read in binary mode and detect encoding rather than trusting MIME headers.
"""

from __future__ import annotations

from pathlib import Path


def read_mht_binary(path: Path) -> bytes:
    """Read a file in binary mode and return the raw bytes."""
    return Path(path).read_bytes()


def decode_mht_bytes(data: bytes) -> str:
    """Decode raw MHT bytes to a string using a robust fallback chain.

    Fallback order:
      1. Declared charset extracted from MIME Content-Type header (first 4 KB only)
      2. utf-8-sig (handles Windows BOM)
      3. windows-1252 (handles localized Windows UI text including Hebrew PSR labels)
      4. utf-8 with errors=replace (final: never raises, never crashes)

    This function MUST NEVER raise an exception on any byte sequence.
    """
    declared = _extract_declared_charset(data)
    candidates = _build_candidate_list(declared)

    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # utf-8 with errors=replace cannot fail on any byte sequence.
    return data.decode("utf-8", errors="replace")


def _extract_declared_charset(data: bytes) -> str | None:
    """Scan the first 4 KB of the MHT for a charset= declaration."""
    header_region = data[:4096].decode("ascii", errors="ignore").lower()
    for fragment in header_region.split(";"):
        fragment = fragment.strip()
        if fragment.startswith("charset="):
            return fragment[len("charset="):].strip().strip('"')
    return None


def _build_candidate_list(declared: str | None) -> list[str]:
    """Build a deduplicated codec list with the declared charset first.

    The list excludes the final utf-8 replace fallback so callers can
    iterate strict decoders and handle the catch-all separately.
    """
    base = ["utf-8-sig", "windows-1252"]
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    for enc in base:
        if enc not in candidates:
            candidates.append(enc)
    return candidates
