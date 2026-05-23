"""Playwright codegen: record browser actions and emit a runnable test file."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]


@dataclass
class CodegenResult:
    url: str
    output_file: Path
    data_json: Path | None = None
    action_count: int = 0

    def report(self) -> str:
        lines = [f"Recorded: {self.url}"]
        lines.append(f"  Test file: {self.output_file}")
        if self.data_json:
            lines.append(f"  Data: {self.data_json}")
        lines.append(f"  Actions captured: {self.action_count}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# record_session (wraps playwright codegen subprocess)
# ---------------------------------------------------------------------------

def record_session(
    url: str,
    output_file: Path,
    browser: str = "chromium",
    output_dir: Path | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> CodegenResult:
    """Launch Playwright codegen, let the user interact, then save the generated test.

    This opens a browser window. When the user closes it, the recorded actions are
    written to `output_file` as a ready-to-run pytest file.
    """
    if sync_playwright is None:
        raise ImportError(
            "Playwright is required for this command.\n"
            "Run: pip install psr-ai[capture]"
        )

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_file.parent.mkdir(parents=True, exist_ok=True)

    _echo(f"  Opening browser at: {url}")
    _echo("  Interact with the page. Close the browser window when done.")
    _echo("  The recorded actions will be saved automatically.")

    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        f"--browser={browser}",
        f"--output={output_file}",
        url,
    ]

    proc = subprocess.run(cmd)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"playwright codegen exited with code {proc.returncode}")

    action_count = 0
    data_json: Path | None = None

    if output_file.exists():
        content = output_file.read_text(encoding="utf-8")
        action_count = len(re.findall(r"\bpage\.\w+\(", content))

        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            data_json = output_dir / "codegen_data.json"
            steps = _extract_steps_from_codegen(content, url)
            data_json.write_text(
                json.dumps(
                    {
                        "url": url,
                        "browser": browser,
                        "action_count": action_count,
                        "steps": steps,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    return CodegenResult(
        url=url,
        output_file=output_file,
        data_json=data_json,
        action_count=action_count,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_steps_from_codegen(source: str, url: str) -> list[dict]:
    """Parse the generated Playwright source into a simple step list."""
    steps: list[dict] = []
    for i, line in enumerate(source.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"page\.(\w+)\((.{0,120})", stripped)
        if m:
            action = m.group(1)
            args = m.group(2).rstrip(");").strip()
            steps.append({
                "index": len(steps),
                "action": action,
                "args": args,
                "source_line": i + 1,
            })
    return steps


# ---------------------------------------------------------------------------
# record_actions_programmatic (headless capture of a scripted sequence)
# ---------------------------------------------------------------------------

def record_actions_programmatic(
    url: str,
    actions: list[dict],
    output_file: Path,
    output_dir: Path | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> CodegenResult:
    """Execute a list of actions programmatically and emit a Playwright test file.

    `actions` is a list of dicts, each with a 'type' key and optional fields:
      - {"type": "click", "selector": "button#submit"}
      - {"type": "fill", "selector": "input[name='q']", "value": "hello"}
      - {"type": "navigate", "url": "https://example.com/page"}
      - {"type": "wait", "ms": 500}
      - {"type": "screenshot", "path": "shot.png"}
    """
    if sync_playwright is None:
        raise ImportError(
            "Playwright is required for this command.\n"
            "Run: pip install psr-ai[capture]"
        )

    def _echo(msg: str) -> None:
        if echo_fn:
            echo_fn(msg)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    code_lines: list[str] = [
        "from playwright.sync_api import sync_playwright",
        "",
        "",
        "def test_recorded_session(page):",
        f'    page.goto("{url}")',
    ]

    recorded_steps: list[dict] = []

    _sp = sync_playwright
    with _sp() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=60_000)

        for idx, action in enumerate(actions):
            atype = action.get("type", "")
            _echo(f"  [{idx + 1}/{len(actions)}] {atype}")

            if atype == "click":
                sel = action["selector"]
                page.click(sel)
                code_lines.append(f'    page.click({sel!r})')
                recorded_steps.append({"index": idx, "action": "click", "selector": sel})

            elif atype == "fill":
                sel = action["selector"]
                val = action.get("value", "")
                page.fill(sel, val)
                code_lines.append(f'    page.fill({sel!r}, {val!r})')
                recorded_steps.append({"index": idx, "action": "fill", "selector": sel, "value": val})

            elif atype == "navigate":
                nav_url = action["url"]
                page.goto(nav_url, wait_until="networkidle", timeout=60_000)
                code_lines.append(f'    page.goto({nav_url!r})')
                recorded_steps.append({"index": idx, "action": "navigate", "url": nav_url})

            elif atype == "wait":
                ms = action.get("ms", 500)
                time.sleep(ms / 1000)
                code_lines.append(f'    page.wait_for_timeout({ms})')
                recorded_steps.append({"index": idx, "action": "wait", "ms": ms})

            elif atype == "screenshot":
                shot_name = action.get("path", f"step_{idx:03d}.png")
                shot_path = (output_dir or output_file.parent) / shot_name
                page.screenshot(path=str(shot_path), full_page=True)
                code_lines.append(f'    page.screenshot(path={str(shot_path)!r}, full_page=True)')
                recorded_steps.append({"index": idx, "action": "screenshot", "path": str(shot_path)})

        browser.close()

    code_lines.append("")
    output_file.write_text("\n".join(code_lines), encoding="utf-8")

    data_json: Path | None = None
    if output_dir:
        data_json = output_dir / "codegen_data.json"
        data_json.write_text(
            json.dumps(
                {"url": url, "action_count": len(recorded_steps), "steps": recorded_steps},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    return CodegenResult(
        url=url,
        output_file=output_file,
        data_json=data_json,
        action_count=len(recorded_steps),
    )
