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


def _whisper_available() -> bool:
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Step 1: download or resolve local file
# ---------------------------------------------------------------------------

def _resolve_source(source: str, work_dir: Path, echo: Callable) -> Path | None:
    """Return a local video file path, downloading if source is a URL."""
    p = Path(source)
    if p.exists():
        return p

    # It's a URL - download with yt-dlp
    echo("  Downloading video...")
    from psr_ai.core.downloader import _ytdlp_cmd, _ytdlp_available
    if not _ytdlp_available():
        raise ImportError("yt-dlp required. Run: pip install psr-ai[download]")

    base_cmd = _ytdlp_cmd()
    out_tmpl = str(work_dir / "video.%(ext)s")
    cmd = base_cmd + [
        "--output", out_tmpl,
        "--no-playlist",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        source,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Find what was downloaded
    candidates = list(work_dir.glob("video.*"))
    if not candidates:
        raise RuntimeError(
            f"Download failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'unknown error'}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Step 2: get video duration
# ---------------------------------------------------------------------------

def _get_duration(video_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Step 3: extract frames with ffmpeg
# ---------------------------------------------------------------------------

def _extract_frames(
    video_path: Path,
    frames_dir: Path,
    fps: float,
    max_frames: int,
    echo: Callable,
) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    echo(f"  Extracting frames at {fps} fps (max {max_frames})...")

    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-frames:v", str(max_frames),
        "-q:v", "2",
        str(frames_dir / "frame_%04d.jpg"),
        "-y", "-loglevel", "error",
    ]
    subprocess.run(cmd, capture_output=True, timeout=120)
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    echo(f"  Extracted {len(frames)} frames.")
    return frames


# ---------------------------------------------------------------------------
# Step 4: transcribe audio with Whisper
# ---------------------------------------------------------------------------

def _transcribe(video_path: Path, echo: Callable) -> str:
    if not _whisper_available():
        echo("  Whisper not available, skipping transcription.")
        return ""

    import whisper
    echo("  Transcribing audio with Whisper (this may take a moment)...")
    model = whisper.load_model("base")
    result = model.transcribe(str(video_path))
    transcript = result.get("text", "").strip()
    echo(f"  Transcript: {len(transcript)} characters.")
    return transcript


# ---------------------------------------------------------------------------
# Step 5: analyze with Claude
# ---------------------------------------------------------------------------

def _analyze_with_claude(
    frames: list[Path],
    transcript: str,
    question: str,
    max_frames_to_send: int,
    echo: Callable,
) -> str:
    import anthropic

    echo(f"  Sending {min(len(frames), max_frames_to_send)} frames + transcript to Claude...")

    content: list[dict] = []

    # Add evenly spaced frames
    if frames:
        step = max(1, len(frames) // max_frames_to_send)
        selected = frames[::step][:max_frames_to_send]
        for frame in selected:
            img_b64 = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_b64,
                },
            })

    # Add transcript
    if transcript:
        content.append({
            "type": "text",
            "text": f"Audio transcript:\n{transcript}\n\n---\n",
        })

    # Add the question
    content.append({
        "type": "text",
        "text": question,
    })

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_video(
    source: str,
    output_dir: Path,
    question: str = (
        "Please analyze this video. Provide:\n"
        "1. TL;DR (2-3 sentences)\n"
        "2. Key points (bullet list)\n"
        "3. Notable quotes or moments\n"
        "4. Main takeaways"
    ),
    fps: float = 0.5,
    max_frames: int = 300,
    whisper_model: str = "base",
    echo_fn: Callable[[str], None] | None = None,
) -> VideoAnalysisResult:
    """Analyze a video from a URL or local file using frames + transcript + Claude.

    Args:
        source: URL (YouTube, TikTok, Instagram, etc.) or local file path.
        output_dir: Directory to save frames, transcript, and analysis.
        question: What to ask Claude about the video.
        fps: Frames per second to extract (0.5 = 1 frame every 2 seconds).
        max_frames: Maximum frames to extract total.
        whisper_model: Whisper model size: tiny, base, small, medium, large.
        echo_fn: Progress callback.

    Requires: pip install psr-ai[analyze]
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

        # Step 1: resolve source to local file
        try:
            _echo(f"  Source: {source}")
            video_path = _resolve_source(source, work_dir, _echo)
        except Exception as exc:
            return VideoAnalysisResult(source=source, output_dir=output_dir, error=str(exc))

        # Step 2: get duration
        duration = _get_duration(video_path)
        if duration > 0:
            _echo(f"  Duration: {duration:.1f}s ({duration/60:.1f} min)")

        # Clamp fps for very long videos to stay under max_frames
        effective_fps = fps
        if duration > 0:
            max_by_duration = max_frames / duration
            effective_fps = min(fps, max_by_duration)

        # Step 3: extract frames
        try:
            frames = _extract_frames(video_path, frames_dir, effective_fps, max_frames, _echo)
        except Exception as exc:
            return VideoAnalysisResult(source=source, output_dir=output_dir, error=str(exc))

        # Step 4: transcribe
        transcript = ""
        try:
            transcript = _transcribe(video_path, _echo)
        except Exception as exc:
            _echo(f"  Transcription failed (continuing without it): {exc}")

        # Save transcript
        if transcript:
            (output_dir / "transcript.txt").write_text(transcript, encoding="utf-8")

    # Step 5: analyze with Claude (frames already saved to frames_dir)
    try:
        summary = _analyze_with_claude(frames, transcript, question, max_frames_to_send=50, echo=_echo)
    except Exception as exc:
        return VideoAnalysisResult(
            source=source, output_dir=output_dir,
            transcript=transcript, frames_extracted=len(frames),
            duration_seconds=duration, error=str(exc),
        )

    # Save analysis
    analysis_path = output_dir / "analysis.md"
    analysis_path.write_text(
        f"# Video Analysis\n\nSource: {source}\n\n## Question\n\n{question}\n\n## Analysis\n\n{summary}\n",
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
