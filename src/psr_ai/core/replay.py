"""Replay a recorded session using Claude as the execution engine.

Instead of replaying actions at exact coordinates (brittle macro style),
Claude sees the current screen at each step and decides how to perform
the action - adapting to UI changes, different resolutions, or reordered
elements.

Usage:
    from psr_ai.core.replay import ReplayConfig, run_replay
    config = ReplayConfig(
        zip_path=Path("session.zip"),
        dry_run=False,
        ask_before_action=False,
    )
    result = run_replay(config)
    print(result.report())
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Config and result
# ---------------------------------------------------------------------------

@dataclass
class ReplayConfig:
    """Parameters for a single replay run."""

    zip_path: Path
    dry_run: bool = True
    ask_before_action: bool = False       # default: fully automatic
    ask_before_destructive: bool = True   # always guard destructive steps
    provider: str = "anthropic"
    model: str | None = None
    max_steps: int = 60                   # replays can be long
    speed: float = 1.0                    # multiplier on screenshot_interval
    output_dir: Path | None = None


@dataclass
class ReplayResult:
    """Outcome of a run_replay() call."""

    zip_path: Path
    success: bool
    session_title: str = ""
    total_session_steps: int = 0
    agent_result: object = None           # AgentResult - typed as object to avoid circular import
    error: str = ""

    def report(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"Replay result: {status}",
            f"  Source:         {self.zip_path.name}",
            f"  Session title:  {self.session_title}",
            f"  Session steps:  {self.total_session_steps}",
        ]
        if self.agent_result is not None:
            ar = self.agent_result
            lines.append(f"  Agent steps:    {len(ar.steps)}")
            lines.append(f"  Stopped:        {ar.stopped_reason}")
            if ar.session_dir:
                lines.append(f"  Log dir:        {ar.session_dir}")
        if self.error:
            lines.append(f"  Error:          {self.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_replay(
    config: ReplayConfig,
    echo_fn: Callable[[str], None] | None = None,
    prompt_fn: Callable[[str], str] | None = None,
) -> ReplayResult:
    """Replay a recorded session using Claude as the execution engine.

    Never raises. All exceptions are caught and written to result.error.

    Args:
      config:    ReplayConfig controlling behaviour.
      echo_fn:   callable for progress output. Defaults to print().
      prompt_fn: callable for user prompts. Defaults to input().

    Returns ReplayResult with the embedded AgentResult.
    """
    echo = echo_fn or print
    result = ReplayResult(zip_path=Path(config.zip_path), success=False)

    try:
        _run(config, result, echo, prompt_fn)
    except KeyboardInterrupt:
        result.error = "Interrupted by user (Ctrl+C)."
        echo("\nReplay interrupted. Stopping cleanly.")
    except Exception as exc:
        result.error = str(exc)
        echo(f"Replay error: {exc}")

    return result


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _load_session_meta(zip_path: Path) -> tuple[str, list[dict], bytes | None]:
    """Read data.json and optionally the first screenshot from the ZIP.

    Returns (title, steps_list, first_screenshot_bytes_or_None).
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid ZIP file: {zip_path.name}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        # Load data.json - present in Mac/Linux ZIPs and in parse output ZIPs.
        data_json_candidates = [n for n in names if n.endswith("data.json")]
        if not data_json_candidates:
            raise ValueError(
                f"No data.json found in {zip_path.name}. "
                "Run `sa stop` or `sa pipeline` first to produce a parsed session."
            )
        raw = zf.read(data_json_candidates[0])
        data = json.loads(raw.decode("utf-8"))

        title = data.get("title", zip_path.stem)
        steps = data.get("steps", [])

        # Try to read the first screenshot for use as initial_screenshot hint.
        first_img: bytes | None = None
        screenshot_candidates = sorted(
            n for n in names
            if n.startswith("screenshots/") and n.endswith(".png")
        )
        if screenshot_candidates:
            try:
                first_img = zf.read(screenshot_candidates[0])
            except Exception:
                pass

    return title, steps, first_img


def _build_task_from_session(title: str, steps: list[dict]) -> str:
    """Convert session metadata into a natural-language task description for Claude."""
    lines = [f"Replay the following recorded workflow: {title}"]
    lines.append("")
    lines.append("Steps to reproduce (in order):")
    for s in steps[:30]:   # cap at 30 to avoid overloading the prompt
        label = s.get("label", "")
        action = s.get("action_text", "") or s.get("label", "")
        program = s.get("program", "")
        if program:
            lines.append(f"  - [{program}] {action or label}")
        else:
            lines.append(f"  - {action or label}")

    if len(steps) > 30:
        lines.append(f"  ... and {len(steps) - 30} more steps")

    lines.append("")
    lines.append(
        "Adapt to the current state of the screen. "
        "If a UI element has moved or changed, find it visually rather than "
        "using exact coordinates. Use 'done' when all steps are complete."
    )
    return "\n".join(lines)


def _run_agent(agent_config, **kwargs):
    """Thin wrapper so tests can patch at 'psr_ai.core.replay._run_agent'."""
    from psr_ai.core.agent import run_agent
    return run_agent(agent_config, **kwargs)


def _run(
    config: ReplayConfig,
    result: ReplayResult,
    echo: Callable[[str], None],
    prompt_fn: Callable[[str], str] | None,
) -> None:
    from psr_ai.core.agent import AgentConfig

    echo(f"Loading session: {config.zip_path.name}")
    title, steps, first_img_bytes = _load_session_meta(Path(config.zip_path))

    result.session_title = title
    result.total_session_steps = len(steps)

    echo(f"Session: {title!r} ({len(steps)} steps)")
    echo("")

    task = _build_task_from_session(title, steps)

    # Write initial screenshot to a temp file if we have one - AgentConfig
    # accepts a Path, not raw bytes.
    initial_screenshot: Path | None = None
    if first_img_bytes:
        from psr_ai.utils.paths import default_output_dir, timestamp_filename
        tmp_dir = default_output_dir() / ".replay_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        initial_screenshot = tmp_dir / timestamp_filename("initial", ".png")
        initial_screenshot.write_bytes(first_img_bytes)

    agent_config = AgentConfig(
        task=task,
        max_steps=config.max_steps,
        provider=config.provider,
        model=config.model,
        dry_run=config.dry_run,
        ask_before_action=config.ask_before_action,
        ask_before_destructive=config.ask_before_destructive,
        screenshot_interval=0.5 * config.speed,
        output_dir=config.output_dir,
        initial_screenshot=initial_screenshot,
    )

    agent_result = _run_agent(agent_config, echo_fn=echo, prompt_fn=prompt_fn)
    result.agent_result = agent_result
    result.success = agent_result.success

    # Clean up temp screenshot.
    if initial_screenshot and initial_screenshot.exists():
        try:
            initial_screenshot.unlink()
        except OSError:
            pass
