"""Video analysis pipeline: download -> extract frames -> transcribe -> analyze with Claude."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class VideoAnalysisResult:
    source: str
    output_dir: Path
    transcript: str = ""
    summary: str = ""
    frames_extracted: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.summary)

    def report(self) -> str:
        if self.error:
            return f"Video analysis failed: {self.error}"
        lines = [f"Video analyzed: {self.source}"]
        lines.append(f"  Duration: {self.duration_seconds:.1f}s")
        lines.append(f"  Frames sampled: {self.frames_extracted}")
        lines.append(f"  Output: {self.output_dir}")
        lines.append("")
        lines.append(self.summary)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _ffmpeg_available() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    except ImportError:
        return False


def _is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Auto-fps: budget frames by duration (ported from claude-video/frames.py)
# ---------------------------------------------------------------------------

_MAX_FPS = 2.0


def _clamp_fps(fps: float, duration: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, _MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration))))
    return fps, target


def _auto_fps(duration: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps targeting a sensible frame budget for full-video scans."""
    if duration <= 0:
        return 1.0, 1
    if duration <= 30:
        target = min(max_frames, max(12, int(round(duration))))
    elif duration <= 60:
        target = min(max_frames, 40)
    elif duration <= 180:
        target = min(max_frames, 60)
    elif duration <= 600:
        target = min(max_frames, 80)
    else:
        target = max_frames
    return _clamp_fps(target / duration, duration, max_frames)


def _auto_fps_focus(duration: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified sub-ranges."""
    if duration <= 0:
        return min(_MAX_FPS, 2.0), 2
    if duration <= 5:
        target = min(max_frames, max(10, int(round(duration * 6))))
    elif duration <= 15:
        target = min(max_frames, max(30, int(round(duration * 4))))
    elif duration <= 30:
        target = min(max_frames, 60)
    elif duration <= 60:
        target = min(max_frames, 80)
    else:
        target = max_frames
    return _clamp_fps(target / duration, duration, max_frames)


# ---------------------------------------------------------------------------
# Step 1: download or resolve local file (with subtitle fetch + injection guard)
# ---------------------------------------------------------------------------

def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
            return candidate
    return None


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [c for c in candidates if ".en" in c.name]
    return preferred[0] if preferred else candidates[0]


def _resolve_source(
    source: str,
    work_dir: Path,
    echo: Callable,
) -> tuple[Path, Path | None]:
    """Return (video_path, subtitle_path|None). Downloads URL if needed."""
    p = Path(source)
    if p.exists():
        return p, None

    echo("  Downloading video...")
    from psr_ai.core.downloader import _ytdlp_cmd, _ytdlp_available
    if not _ytdlp_available():
        raise ImportError("yt-dlp required. Run: pip install psr-ai[download]")

    base_cmd = _ytdlp_cmd()
    out_tmpl = str(work_dir / "video.%(ext)s")
    cmd = base_cmd + [
        "-N", "4",
        "-f", "bv*[height<=720]+ba/b[height<=720]/bv+ba/b",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en,en-US,en-GB,en-orig",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", out_tmpl,
        "--",   # injection guard: everything after this is a positional argument
        source,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    video = _pick_video(work_dir)
    if video is None:
        last = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
        raise RuntimeError(f"Download failed: {last}")

    subtitle = _pick_subtitle(work_dir)
    if subtitle:
        echo(f"  Native captions found: {subtitle.name}")
    return video, subtitle


# ---------------------------------------------------------------------------
# Step 2: get video metadata
# ---------------------------------------------------------------------------

def _get_metadata(video_path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        vs = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {
            "duration_seconds": float(fmt.get("duration") or vs.get("duration") or 0),
            "width": vs.get("width"),
            "height": vs.get("height"),
        }
    except Exception:
        return {"duration_seconds": 0.0, "width": None, "height": None}


# ---------------------------------------------------------------------------
# Step 3: extract frames with auto-fps + 512px resize
# ---------------------------------------------------------------------------

def _extract_frames(
    video_path: Path,
    frames_dir: Path,
    duration: float,
    max_frames: int,
    echo: Callable,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    resolution: int = 512,
) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)

    focused = start_seconds is not None or end_seconds is not None
    effective_start = start_seconds or 0.0
    effective_end = end_seconds or duration
    effective_duration = max(0.0, effective_end - effective_start)

    if focused:
        fps, target = _auto_fps_focus(effective_duration, max_frames)
    else:
        fps, target = _auto_fps(duration, max_frames)

    scope = (
        f"{effective_duration:.1f}s focused"
        if focused else f"{duration:.1f}s full"
    )
    echo(f"  Extracting ~{target} frames at {fps:.3f} fps over {scope}...")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-i", str(video_path),
        "-vf", f"fps={fps},scale={resolution}:-2",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        str(frames_dir / "frame_%04d.jpg"),
    ]

    subprocess.run(cmd, capture_output=True, timeout=120)
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    echo(f"  Extracted {len(frames)} frames.")
    return frames


# ---------------------------------------------------------------------------
# Step 4: transcribe — native captions first, Whisper API fallback
# ---------------------------------------------------------------------------

def _parse_vtt(vtt_path: Path) -> str:
    """Extract plain text from a WebVTT file."""
    import re
    lines = vtt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    text_lines: list[str] = []
    skip_header = True
    for line in lines:
        if skip_header:
            if line.strip() == "" and text_lines == []:
                continue
            if "-->" in line:
                skip_header = False
            continue
        if "-->" in line:
            continue
        cleaned = re.sub(r"<[^>]+>", "", line).strip()
        if cleaned and (not text_lines or text_lines[-1] != cleaned):
            text_lines.append(cleaned)
    return " ".join(text_lines)


def _transcribe_whisper_api(video_path: Path, work_dir: Path, echo: Callable) -> str:
    """Transcribe via Groq or OpenAI Whisper API (no local model needed)."""
    groq_key = os.environ.get("GROQ_API_KEY") or _read_watch_env("GROQ_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY") or _read_watch_env("OPENAI_API_KEY")

    if not groq_key and not openai_key:
        return ""

    audio_path = work_dir / "audio.mp3"
    echo("  Extracting audio for Whisper...")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
         str(audio_path)],
        capture_output=True, timeout=120,
    )
    if not audio_path.exists():
        return ""

    if groq_key:
        try:
            import httpx
            echo("  Transcribing via Groq Whisper...")
            with open(audio_path, "rb") as f:
                resp = httpx.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    data={"model": "whisper-large-v3", "response_format": "text"},
                    files={"file": ("audio.mp3", f, "audio/mpeg")},
                    timeout=120,
                )
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception as exc:
            echo(f"  Groq Whisper failed: {exc}")

    if openai_key:
        try:
            import httpx
            echo("  Transcribing via OpenAI Whisper...")
            with open(audio_path, "rb") as f:
                resp = httpx.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    data={"model": "whisper-1", "response_format": "text"},
                    files={"file": ("audio.mp3", f, "audio/mpeg")},
                    timeout=120,
                )
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception as exc:
            echo(f"  OpenAI Whisper failed: {exc}")

    return ""


def _read_watch_env(key: str) -> str | None:
    """Read a key from ~/.config/watch/.env (shared with /watch skill)."""
    env_file = Path.home() / ".config" / "watch" / ".env"
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                v = v.strip().strip("\"'")
                return v or None
    except OSError:
        return None
    return None


def _transcribe(
    video_path: Path,
    work_dir: Path,
    subtitle_path: Path | None,
    echo: Callable,
) -> tuple[str, str]:
    """Return (transcript_text, source_label). Tries captions → API → local Whisper."""
    # 1. Native captions (free, instant)
    if subtitle_path and subtitle_path.exists():
        try:
            text = _parse_vtt(subtitle_path)
            if text:
                echo(f"  Transcript from native captions ({len(text)} chars).")
                return text, "captions"
        except Exception as exc:
            echo(f"  Caption parse failed: {exc}")

    # 2. Whisper API (Groq preferred, OpenAI fallback)
    api_text = _transcribe_whisper_api(video_path, work_dir, echo)
    if api_text:
        return api_text, "whisper-api"

    # 3. faster-whisper local (4x faster than openai/whisper, no API key needed)
    try:
        from faster_whisper import WhisperModel
        echo("  Transcribing with faster-whisper (local, auto-detect language)...")
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(
            str(video_path),
            language=None,       # auto-detect: Hebrew, Arabic, English, etc.
            beam_size=5,
            vad_filter=True,     # skip silence → no hallucinations
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            echo(f"  Detected language: {info.language} ({info.language_probability:.0%})")
            echo(f"  Transcript from faster-whisper ({len(text)} chars).")
            return text, f"faster-whisper ({info.language})"
    except ImportError:
        pass
    except Exception as exc:
        echo(f"  faster-whisper failed: {exc}")

    # 4. openai/whisper local (legacy fallback)
    try:
        import whisper
        echo("  Transcribing with openai/whisper (this may take a moment)...")
        model = whisper.load_model("base")
        result = model.transcribe(str(video_path))
        text = result.get("text", "").strip()
        if text:
            echo(f"  Transcript from openai/whisper ({len(text)} chars).")
            return text, "whisper-local"
    except ImportError:
        pass
    except Exception as exc:
        echo(f"  Local Whisper failed: {exc}")

    echo("  No transcript available (no captions, no Whisper key, no local Whisper).")
    return "", "none"


# ---------------------------------------------------------------------------
# Step 5: analyze with Claude
# ---------------------------------------------------------------------------

_DEFAULT_QUESTION = (
    "Please analyze this video. Provide:\n"
    "1. TL;DR (2-3 sentences)\n"
    "2. Key points (bullet list)\n"
    "3. Notable quotes or moments\n"
    "4. Main takeaways"
)


def _analyze_with_claude(
    frames: list[dict],
    transcript: str,
    transcript_source: str,
    question: str,
    max_frames_to_send: int,
    echo: Callable,
) -> str:
    import anthropic

    step = max(1, len(frames) // max_frames_to_send)
    selected = frames[::step][:max_frames_to_send]
    echo(f"  Sending {len(selected)} frames + transcript ({transcript_source}) to Claude...")

    # System prompt: structured summary vs. direct answer based on question type
    is_summary_request = question.strip() == _DEFAULT_QUESTION.strip()
    if is_summary_request:
        system = (
            "You are a video analysis assistant. "
            "Respond in the same language as the video's spoken content or on-screen text. "
            "Use clear Markdown headings and bullet points. "
            "Each frame label shows its timestamp (t=MM:SS) — cite timestamps when relevant."
        )
    else:
        system = (
            "You are a video analysis assistant. "
            "Answer the user's question directly and concisely, in the same language they asked. "
            "Each frame label shows its timestamp (t=MM:SS) — cite timestamps when relevant."
        )

    content: list[dict] = []

    # Frames with timestamps (שיפור 4)
    for frame in selected:
        ts = format_time(frame["timestamp_seconds"])
        content.append({"type": "text", "text": f"[t={ts}]"})
        img_b64 = base64.b64encode(Path(frame["path"]).read_bytes()).decode("ascii")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })

    if transcript:
        content.append({
            "type": "text",
            "text": f"Audio transcript (source: {transcript_source}):\n{transcript}\n\n---\n",
        })

    content.append({"type": "text", "text": question})

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_video(
    source: str,
    output_dir: Path,
    question: str = _DEFAULT_QUESTION,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    resolution: int = 512,
    echo_fn: Callable[[str], None] | None = None,
) -> VideoAnalysisResult:
    """Analyze a video from a URL or local file using frames + transcript + Claude.

    Args:
        source: URL (YouTube, TikTok, Instagram, etc.) or local file path.
        output_dir: Directory to save frames, transcript, and analysis.
        question: What to ask Claude about the video.
        max_frames: Maximum frames to extract (auto-fps scales to fit budget).
        start_seconds: Optional range start for focused analysis.
        end_seconds: Optional range end for focused analysis.
        resolution: Frame width in pixels (default 512 — keeps token cost low).
        echo_fn: Progress callback.
    """
    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    if not _ffmpeg_available():
        return VideoAnalysisResult(
            source=source, output_dir=output_dir,
            error="ffmpeg is required. Download from https://ffmpeg.org/download.html",
        )

    if not _anthropic_available():
        return VideoAnalysisResult(
            source=source, output_dir=output_dir,
            error="ANTHROPIC_API_KEY environment variable not set.",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)

        # Step 1: resolve source
        try:
            _echo(f"  Source: {source}")
            video_path, subtitle_path = _resolve_source(source, work_dir, _echo)
        except Exception as exc:
            return VideoAnalysisResult(source=source, output_dir=output_dir, error=str(exc))

        # Step 2: metadata
        meta = _get_metadata(video_path)
        duration = meta["duration_seconds"]
        if duration > 0:
            _echo(f"  Duration: {duration:.1f}s ({duration / 60:.1f} min)")
        if meta.get("width") and meta.get("height"):
            _echo(f"  Resolution: {meta['width']}x{meta['height']}")
        if duration > 1200 and not (start_seconds or end_seconds):
            _echo(
                f"  Warning: {duration / 60:.0f}-min video — frame coverage is sparse. "
                "For better accuracy, re-run with start_seconds/end_seconds to focus on a section."
            )

        # Step 3: extract frames
        try:
            frames = _extract_frames(
                video_path, frames_dir, duration, max_frames, _echo,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                resolution=resolution,
            )
        except Exception as exc:
            return VideoAnalysisResult(source=source, output_dir=output_dir, error=str(exc))

        # Step 4: transcribe
        transcript, transcript_source = "", "none"
        try:
            transcript, transcript_source = _transcribe(
                video_path, work_dir, subtitle_path, _echo
            )
        except Exception as exc:
            _echo(f"  Transcription failed (continuing without it): {exc}")

        if transcript:
            (output_dir / "transcript.txt").write_text(transcript, encoding="utf-8")

    # Step 5: analyze with Claude
    try:
        summary = _analyze_with_claude(
            frames, transcript, transcript_source, question,
            max_frames_to_send=50, echo=_echo,
        )
    except Exception as exc:
        return VideoAnalysisResult(
            source=source, output_dir=output_dir,
            transcript=transcript, frames_extracted=len(frames),
            duration_seconds=duration, error=str(exc),
        )

    analysis_path = output_dir / "analysis.md"
    analysis_path.write_text(
        f"# Video Analysis\n\nSource: {source}\n\n"
        f"## Question\n\n{question}\n\n"
        f"## Analysis\n\n{summary}\n",
        encoding="utf-8",
    )
    _echo(f"  Analysis saved to {analysis_path}")

    return VideoAnalysisResult(
        source=source,
        output_dir=output_dir,
        transcript=transcript,
        summary=summary,
        frames_extracted=len(frames),
        duration_seconds=duration,
    )
