"""Charset detection for MHT files that lack or lie about their encoding.

PSR produces MHT files with inconsistent or absent charset declarations (Pitfall 1).
Always read in binary mode and detect encoding rather than trusting MIME headers.
"""

from __future__ import annotations


# Ordered list of encodings to try before giving up.
# utf-8-sig handles the Windows BOM that PSR sometimes prepends.
_FALLBACK_ENCODINGS = ["utf-8-sig", "utf-8", "windows-1252", "latin-1"]


def decode_mht_bytes(data: bytes) -> str:
    """Decode raw MHT bytes to a string, trying common encodings in order.

    The function first attempts to extract the charset from the MIME Content-Type
    header. If that fails or the declared codec raises an error, it falls through
    the fallback list. latin-1 is the final fallback because it decodes every
    byte without raising, though with possible mojibake for non-Latin scripts.
    """
    declared = _extract_declared_charset(data)
    candidates = _build_candidate_list(declared)

    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # latin-1 cannot fail on any byte sequence; this line is always reached if
    # every other codec errors out.
    return data.decode("latin-1", errors="replace")


def _extract_declared_charset(data: bytes) -> str | None:
    """Scan the first 4 KB of the MHT for a charset= declaration."""
    header_region = data[:4096].decode("ascii", errors="ignore").lower()
    for fragment in header_region.split(";"):
        fragment = fragment.strip()
        if fragment.startswith("charset="):
            return fragment[len("charset="):].strip().strip('"')
    return None


def _build_candidate_list(declared: str | None) -> list[str]:
    """Return a deduplicated codec list with the declared charset first."""
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    for enc in _FALLBACK_ENCODINGS:
        if enc not in candidates:
            candidates.append(enc)
    return candidates
