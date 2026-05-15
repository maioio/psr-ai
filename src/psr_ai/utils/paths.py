"""Safe path helpers - all file operations must go through these functions.

Rules enforced here:
- Never use string concatenation for paths; always use pathlib.Path.
- Sanitize user-supplied filenames to strip characters illegal on Windows
  (: * ? " < > | and leading/trailing whitespace).
- safe_path() is the single guard against path traversal for user-supplied inputs.
"""

from __future__ import annotations

import re
from pathlib import Path


# Characters that Windows forbids in file or directory names.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_path(user_input: str | Path, base: Path | None = None) -> Path:
    """Resolve a user-supplied path, preventing traversal outside base.

    If base is provided the resolved path must remain inside it.
    Raises ValueError for inputs that escape the base directory.
    """
    path = Path(user_input).expanduser().resolve()
    if base is not None:
        base = base.expanduser().resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"Path '{path}' is outside the allowed base directory '{base}'."
            ) from exc
    return path


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are illegal in Windows filenames."""
    cleaned = _WINDOWS_ILLEGAL.sub("_", name)
    # Collapse multiple consecutive underscores and trim edges.
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unnamed"


def default_output_dir(source: Path | None = None) -> Path:
    """Return the default output directory for a recording session.

    If source is given, the output sits next to it.
    Otherwise falls back to ./psr_output/ in the current working directory.
    """
    if source is not None:
        return source.parent / (source.stem + "_output")
    return Path.cwd() / "psr_output"
