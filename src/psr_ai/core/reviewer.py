"""Interactive privacy review: step-by-step keep/drop workflow for a session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


def run_review_session(
    source_dir: Path,
    *,
    prompt_fn: Callable[[str], str] | None = None,
    echo_fn: Callable[[str], None] | None = None,
    regenerate: bool = True,
) -> tuple[int, int]:
    """Iterate through data.json and let the user keep or drop each step.

    For each step the user sees: timestamp, program name, and action text.
    They are asked: "Keep this step or drop due to sensitive data? [K/d]:"
      - K (or Enter): keep the step unchanged.
      - d (or D): purge the step from data.json and delete its PNG screenshot.

    After the review loop the function rewrites data.json with the surviving
    steps and optionally re-generates report.md and action_replay.gif.

    Parameters:
      source_dir  - output directory containing data.json and screenshots/.
      prompt_fn   - callable(str)->str used for input. Defaults to the
                    built-in input(). Injected for testing.
      echo_fn     - callable(str)->None used for output. Defaults to print().
                    Injected for testing.
      regenerate  - when True, regenerate report.md after the review.

    Returns:
      (kept_count, dropped_count) tuple.
    """
    source_dir = Path(source_dir)
    data_path = source_dir / "data.json"
    screenshots_dir = source_dir / "screenshots"

    if not data_path.exists():
        raise FileNotFoundError(
            f"data.json not found in {source_dir}. "
            "Run `sa stop` or `sa pub` first to generate the session data."
        )

    _prompt = prompt_fn if prompt_fn is not None else _default_prompt
    _echo = echo_fn if echo_fn is not None else print

    data = json.loads(data_path.read_text(encoding="utf-8"))
    steps: list[dict] = data.get("steps", [])

    if not steps:
        _echo("No steps found in data.json. Nothing to review.")
        return 0, 0

    total = len(steps)
    _echo(f"\nPrivacy Review - {total} steps to inspect.")
    _echo("For each step: press Enter or K to keep, D to drop.\n")

    kept: list[dict] = []
    dropped_count = 0

    for i, step in enumerate(steps):
        index = step.get("index", i)
        ts = step.get("timestamp") or "no timestamp"
        prog = step.get("program") or "unknown program"
        action = step.get("action_text") or step.get("label") or "no description"
        screenshot = step.get("screenshot", "")

        _echo(f"--- Step {index} of {total - 1} ---")
        _echo(f"  Time:    {ts}")
        _echo(f"  Program: {prog}")
        _echo(f"  Action:  {action}")
        if screenshot:
            _echo(f"  Screenshot: {screenshot}")

        answer = _prompt("  Keep this step or drop due to sensitive data? [K/d]: ").strip().lower()

        if answer in ("d", "drop"):
            _drop_step(step, screenshots_dir, _echo)
            dropped_count += 1
        else:
            kept.append(step)
            _echo("  Kept.")

        _echo("")

    # Rewrite data.json with surviving steps only.
    data["steps"] = kept
    data["step_count"] = len(kept)
    data_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _echo(f"Review complete: {len(kept)} kept, {dropped_count} dropped.")

    if regenerate and kept:
        _regenerate(source_dir, data_path, _echo)

    return len(kept), dropped_count


def _drop_step(step: dict, screenshots_dir: Path, echo_fn) -> None:
    """Remove the step's PNG from disk. Prints confirmation to echo_fn."""
    screenshot_rel = step.get("screenshot", "")
    if screenshot_rel:
        png_path = screenshots_dir / Path(screenshot_rel).name
        if png_path.exists():
            png_path.unlink()
            echo_fn(f"  Dropped + deleted: {png_path.name}")
            return
    echo_fn("  Dropped (no screenshot to delete).")


def _regenerate(source_dir: Path, data_path: Path, echo_fn) -> None:
    """Re-render report.md from the pruned data.json."""
    try:
        from psr_ai.core.generator import MarkdownGenerator
        report_path = MarkdownGenerator(data_path).write(source_dir)
        echo_fn(f"Report regenerated: {report_path}")
    except Exception as exc:
        echo_fn(f"Warning: could not regenerate report ({exc}).")


def _default_prompt(message: str) -> str:
    return input(message)
