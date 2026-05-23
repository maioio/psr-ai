"""Media downloader: yt-dlp for video/audio, gallery-dl for image galleries."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    url: str
    output_dir: Path
    files: list[Path] = field(default_factory=list)
    tool_used: str = ""
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and len(self.files) > 0

    def report(self) -> str:
        if self.error:
            return f"Download failed: {self.error}"
        lines = [f"Downloaded: {self.url}"]
        lines.append(f"  Tool: {self.tool_used}")
        lines.append(f"  Files: {len(self.files)}")
        for f in self.files[:5]:
            lines.append(f"    {f.name}")
        if len(self.files) > 5:
            lines.append(f"    ... and {len(self.files) - 5} more")
        lines.append(f"  Output: {self.output_dir}")
        return "\n".join(lines)


@dataclass
class ProfileDownloadResult:
    url: str
    output_dir: Path
    files: list[Path] = field(default_factory=list)
    tool_used: str = ""
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    def report(self) -> str:
        if self.error:
            return f"Profile download failed: {self.error}"
        lines = [f"Profile downloaded: {self.url}"]
        lines.append(f"  Tool: {self.tool_used}")
        lines.append(f"  Files: {len(self.files)}")
        lines.append(f"  Output: {self.output_dir}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ytdlp_available() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _gallery_dl_available() -> bool:
    try:
        subprocess.run(
            ["gallery-dl", "--version"],
            capture_output=True, timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _files_in(directory: Path) -> list[Path]:
    """Return all files recursively in a directory."""
    return [p for p in directory.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------

def download_media(
    url: str,
    output_dir: Path,
    format_str: str = "bestvideo+bestaudio/best",
    audio_only: bool = False,
    subtitles: bool = False,
    quality: str | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> DownloadResult:
    """Download a video/audio URL using yt-dlp.

    Falls back to gallery-dl for image galleries if yt-dlp fails.
    """
    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    if not _ytdlp_available():
        raise ImportError(
            "yt-dlp is required for this command.\n"
            "Run: pip install psr-ai[download]"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    before_files = set(_files_in(output_dir))

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--output", str(output_dir / "%(uploader)s - %(title)s.%(ext)s"),
        "--no-playlist",
    ]

    if audio_only:
        cmd += ["-x", "--audio-format", "mp3"]
    else:
        if quality:
            cmd += ["-f", f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"]
        else:
            cmd += ["-f", format_str]

    if subtitles:
        cmd += ["--write-sub", "--write-auto-sub", "--sub-lang", "en"]

    cmd.append(url)

    _echo(f"  Downloading: {url}")
    proc = subprocess.run(cmd, capture_output=True, text=True)

    new_files = sorted(set(_files_in(output_dir)) - before_files)

    if proc.returncode != 0 and not new_files:
        # Attempt gallery-dl fallback for image sites
        if _gallery_dl_available():
            _echo("  yt-dlp failed, trying gallery-dl...")
            return _gallery_dl_download(url, output_dir, before_files, echo_fn)
        error = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown error"
        return DownloadResult(url=url, output_dir=output_dir, error=error, tool_used="yt-dlp")

    return DownloadResult(
        url=url,
        output_dir=output_dir,
        files=new_files,
        tool_used="yt-dlp",
    )


# ---------------------------------------------------------------------------
# download_profile
# ---------------------------------------------------------------------------

def download_profile(
    url: str,
    output_dir: Path,
    limit: int | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> ProfileDownloadResult:
    """Download all media from a user profile/channel using gallery-dl or yt-dlp."""
    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    before_files = set(_files_in(output_dir))

    if _gallery_dl_available():
        cmd = ["gallery-dl", "--directory", str(output_dir)]
        if limit:
            cmd += ["--range", f"1-{limit}"]
        cmd.append(url)
        tool = "gallery-dl"
    elif _ytdlp_available():
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--output", str(output_dir / "%(uploader)s/%(title)s.%(ext)s"),
        ]
        if limit:
            cmd += ["--max-downloads", str(limit)]
        cmd.append(url)
        tool = "yt-dlp"
    else:
        raise ImportError(
            "gallery-dl or yt-dlp is required for this command.\n"
            "Run: pip install psr-ai[download]"
        )

    _echo(f"  Downloading profile: {url}")
    _echo(f"  Tool: {tool}")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    new_files = sorted(set(_files_in(output_dir)) - before_files)

    if proc.returncode != 0 and not new_files:
        error = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown error"
        return ProfileDownloadResult(url=url, output_dir=output_dir, error=error, tool_used=tool)

    return ProfileDownloadResult(
        url=url,
        output_dir=output_dir,
        files=new_files,
        tool_used=tool,
    )


# ---------------------------------------------------------------------------
# Internal gallery-dl helper
# ---------------------------------------------------------------------------

def _gallery_dl_download(
    url: str,
    output_dir: Path,
    before_files: set,
    echo_fn: Callable[[str], None] | None = None,
) -> DownloadResult:
    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    cmd = ["gallery-dl", "--directory", str(output_dir), url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    new_files = sorted(set(_files_in(output_dir)) - before_files)

    if proc.returncode != 0 and not new_files:
        error = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "Unknown error"
        return DownloadResult(url=url, output_dir=output_dir, error=error, tool_used="gallery-dl")

    return DownloadResult(
        url=url,
        output_dir=output_dir,
        files=new_files,
        tool_used="gallery-dl",
    )
