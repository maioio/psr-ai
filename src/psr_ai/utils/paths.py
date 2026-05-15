"""Safe path helpers - all file operations must go through these functions.

Rules enforced here:
- Never use string concatenation for paths; always use pathlib.Path.
- Sanitize user-supplied filenames to strip characters illegal on Windows
  (: * ? " < > | and control characters 0x00-0x1f).
- Colons are the most common offender: they appear in ISO-8601 timestamps
  (e.g. "2024-01-15T14:30:00") and Windows forbids them in filenames.
- safe_path() is the single traversal guard for all user-supplied inputs.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


# All characters Windows forbids in file or directory name components.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Reserved Windows device names that must not be used as filenames.
_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def sanitize_filename(name: str) -> str:
    """Return a Windows-safe filename derived from name.

    Replaces every character illegal on Windows (including colons from
    timestamps) with an underscore, collapses runs of underscores, strips
    leading/trailing underscores and dots, and rejects Windows reserved names.

    Examples:
      "psr_2024-01-15T14:30:00"  ->  "psr_2024-01-15T14_30_00"
      "step:01?output*"          ->  "step_01_output"
      "CON"                      ->  "unnamed"
    """
    cleaned = _WINDOWS_ILLEGAL.sub("_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_").rstrip(".")
    if not cleaned or cleaned.upper() in _RESERVED_NAMES:
        return "unnamed"
    return cleaned


def timestamp_filename(prefix: str = "psr", ext: str = ".zip") -> str:
    """Return a safe timestamped filename.

    Timestamps use underscores instead of colons so the result is legal on
    Windows without further sanitization.

    Example: "psr_20240115_143000.zip"
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{sanitize_filename(prefix)}_{ts}{ext}"


def safe_path(user_input: str | Path, base: Path | None = None) -> Path:
    """Resolve a user-supplied path, preventing traversal outside base.

    Expands ~ and resolves symlinks before the containment check.
    Raises ValueError when the resolved path escapes base.
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


def resolve_env_path(env_var: str, *parts: str) -> Path:
    """Build a path from an environment variable and optional sub-parts.

    Uses os.environ rather than os.path.expandvars so failures are explicit.
    Raises KeyError if the variable is unset.

    Example: resolve_env_path("SystemRoot", "System32", "psr.exe")
             -> Path("C:/Windows/System32/psr.exe")
    """
    root = os.environ[env_var]
    return Path(root).joinpath(*parts)


def default_output_dir(source: Path | None = None) -> Path:
    """Return the default output directory for a recording session.

    When source is given the output directory sits next to it.
    Otherwise falls back to ./psr_output/ relative to the current directory.
    """
    if source is not None:
        return source.parent / (source.stem + "_output")
    return Path.cwd() / "psr_output"
