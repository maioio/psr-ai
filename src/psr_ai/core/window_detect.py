"""Detect video-playing windows on the desktop (Windows-only, zero dependencies).

Used by `sa stop` to offer automatic video analysis when a media player or
video tab is open during a PSR recording session.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Patterns: (regex, program_tag, url_extract_group_or_None)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, str, int | None]] = [
    # YouTube in browser: "Title - YouTube" or "Title - YouTube - Google Chrome"
    (r"^(.+?) - YouTube", "youtube", None),
    # youtu.be short links in title
    (r"youtu\.be/([A-Za-z0-9_-]+)", "youtube", None),
    # VLC
    (r"(.+?) - VLC media player", "vlc", None),
    # Windows Media Player
    (r"^Windows Media Player$", "wmp", None),
    # MPC-HC / MPC-BE
    (r"(.+?) - MPC-(?:HC|BE)", "mpc", None),
    # PotPlayer
    (r"(.+?) - PotPlayer", "potplayer", None),
    # Local video files open in any app
    (r"[\\/]([^\\/]+\.(?:mp4|mkv|avi|mov|webm|m4v|flv))\b", "player", None),
    # Vimeo
    (r"(.+?) on Vimeo", "vimeo", None),
    # Twitch
    (r"(.+?) - Twitch", "twitch", None),
    # TikTok
    (r"TikTok", "tiktok", None),
    # Instagram Reels
    (r"Instagram", "instagram", None),
    # DailyMotion
    (r"(.+?) - Dailymotion", "dailymotion", None),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), tag, grp) for p, tag, grp in _PATTERNS]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class VideoWindow:
    title: str
    program: str        # "youtube", "vlc", "wmp", etc.
    suggest_url: str    # URL or file path hint for video-analyze


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_video_windows() -> list[VideoWindow]:
    """Return open windows that appear to contain a video.

    Windows-only (uses ctypes). Returns [] on macOS/Linux without error.
    """
    if sys.platform != "win32":
        return []
    try:
        return _detect_windows_win32()
    except Exception:
        return []


def _detect_windows_win32() -> list[VideoWindow]:
    import ctypes
    import ctypes.wintypes

    titles: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title:
            titles.append(title)
        return True

    ctypes.windll.user32.EnumWindows(_enum_cb, 0)

    results: list[VideoWindow] = []
    seen: set[str] = set()

    for title in titles:
        for pattern, tag, _grp in _COMPILED:
            if pattern.search(title):
                key = f"{tag}:{title[:60]}"
                if key in seen:
                    break
                seen.add(key)
                suggest = _build_suggest_url(title, tag)
                results.append(VideoWindow(title=title, program=tag, suggest_url=suggest))
                break  # one match per window

    return results


def _build_suggest_url(title: str, tag: str) -> str:
    """Build the most useful URL/path hint from a window title."""
    if tag == "youtube":
        # "Video Title - YouTube — Google Chrome" → we can't get the URL from title alone
        return "https://www.youtube.com  (open browser and copy URL)"
    if tag in ("vimeo", "twitch", "tiktok", "instagram", "dailymotion"):
        return f"(open browser, copy the {tag} URL)"
    if tag in ("vlc", "mpc", "potplayer", "player", "wmp"):
        # Try to extract a file path from the title
        m = re.search(r'([A-Za-z]:\\[^\|"<>]+\.(?:mp4|mkv|avi|mov|webm|m4v|flv))', title, re.IGNORECASE)
        if m:
            return m.group(1)
        return "(local file — run sa video-analyze <path>)"
    return title
