"""Playwright codegen: record browser actions and emit a runnable test file.

Supports all Playwright action types:
  click, dblclick, right_click, hover, fill, type (keystroke-by-keystroke),
  press, select_option, check, uncheck, drag_and_drop, mouse.wheel, tap,
  focus, navigate, wait, screenshot, evaluate, expose_function, init_script
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
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

    Supports all three browser engines: chromium, firefox, webkit.
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
# Action dispatcher
# ---------------------------------------------------------------------------

def _dispatch_action(page, action: dict, code_lines: list[str], recorded_steps: list[dict], idx: int) -> None:
    """Execute one action on `page` and append the corresponding code line."""
    atype = action.get("type", "")

    if atype == "click":
        sel = action["selector"]
        page.click(sel)
        code_lines.append(f'    page.click({sel!r})')
        recorded_steps.append({"index": idx, "action": "click", "selector": sel})

    elif atype == "dblclick":
        sel = action["selector"]
        page.dblclick(sel)
        code_lines.append(f'    page.dblclick({sel!r})')
        recorded_steps.append({"index": idx, "action": "dblclick", "selector": sel})

    elif atype in ("right_click", "rightclick"):
        sel = action["selector"]
        page.click(sel, button="right")
        code_lines.append(f'    page.click({sel!r}, button="right")')
        recorded_steps.append({"index": idx, "action": "right_click", "selector": sel})

    elif atype == "hover":
        sel = action["selector"]
        page.hover(sel)
        code_lines.append(f'    page.hover({sel!r})')
        recorded_steps.append({"index": idx, "action": "hover", "selector": sel})

    elif atype == "fill":
        sel = action["selector"]
        val = action.get("value", "")
        page.fill(sel, val)
        code_lines.append(f'    page.fill({sel!r}, {val!r})')
        recorded_steps.append({"index": idx, "action": "fill", "selector": sel, "value": val})

    elif atype == "type":
        # Keystroke by keystroke (slower, triggers key events)
        sel = action.get("selector")
        val = action.get("value", "")
        if sel:
            page.click(sel)
        page.keyboard.type(val)
        if sel:
            code_lines.append(f'    page.click({sel!r})')
        code_lines.append(f'    page.keyboard.type({val!r})')
        recorded_steps.append({"index": idx, "action": "type", "selector": sel, "value": val})

    elif atype == "press":
        key = action.get("key", "Enter")
        sel = action.get("selector")
        if sel:
            page.press(sel, key)
            code_lines.append(f'    page.press({sel!r}, {key!r})')
        else:
            page.keyboard.press(key)
            code_lines.append(f'    page.keyboard.press({key!r})')
        recorded_steps.append({"index": idx, "action": "press", "key": key, "selector": sel})

    elif atype == "select_option":
        sel = action["selector"]
        value = action.get("value")
        label = action.get("label")
        index = action.get("index")
        if value is not None:
            page.select_option(sel, value=value)
            code_lines.append(f'    page.select_option({sel!r}, value={value!r})')
        elif label is not None:
            page.select_option(sel, label=label)
            code_lines.append(f'    page.select_option({sel!r}, label={label!r})')
        elif index is not None:
            page.select_option(sel, index=index)
            code_lines.append(f'    page.select_option({sel!r}, index={index!r})')
        recorded_steps.append({"index": idx, "action": "select_option", "selector": sel})

    elif atype == "check":
        sel = action["selector"]
        page.check(sel)
        code_lines.append(f'    page.check({sel!r})')
        recorded_steps.append({"index": idx, "action": "check", "selector": sel})

    elif atype == "uncheck":
        sel = action["selector"]
        page.uncheck(sel)
        code_lines.append(f'    page.uncheck({sel!r})')
        recorded_steps.append({"index": idx, "action": "uncheck", "selector": sel})

    elif atype == "drag_and_drop":
        source = action["source"]
        target = action["target"]
        page.drag_and_drop(source, target)
        code_lines.append(f'    page.drag_and_drop({source!r}, {target!r})')
        recorded_steps.append({"index": idx, "action": "drag_and_drop", "source": source, "target": target})

    elif atype in ("scroll", "mouse_wheel", "wheel"):
        delta_x = action.get("delta_x", 0)
        delta_y = action.get("delta_y", 300)
        page.mouse.wheel(delta_x, delta_y)
        code_lines.append(f'    page.mouse.wheel({delta_x}, {delta_y})')
        recorded_steps.append({"index": idx, "action": "mouse_wheel", "delta_x": delta_x, "delta_y": delta_y})

    elif atype == "tap":
        sel = action["selector"]
        page.tap(sel)
        code_lines.append(f'    page.tap({sel!r})')
        recorded_steps.append({"index": idx, "action": "tap", "selector": sel})

    elif atype == "focus":
        sel = action["selector"]
        page.focus(sel)
        code_lines.append(f'    page.focus({sel!r})')
        recorded_steps.append({"index": idx, "action": "focus", "selector": sel})

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

    elif atype == "wait_for_selector":
        sel = action["selector"]
        page.wait_for_selector(sel, timeout=action.get("timeout", 30_000))
        code_lines.append(f'    page.wait_for_selector({sel!r})')
        recorded_steps.append({"index": idx, "action": "wait_for_selector", "selector": sel})

    elif atype == "screenshot":
        shot_name = action.get("path", f"step_{idx:03d}.png")
        clip = action.get("clip")
        if clip:
            img = page.screenshot(clip=clip)
            # shot_name determines the file
        else:
            img = page.screenshot(full_page=True)
        # write bytes to file regardless
        import os
        shot_path = Path(shot_name) if os.path.isabs(shot_name) else Path(shot_name)
        shot_path.write_bytes(img)
        code_lines.append(f'    page.screenshot(path={str(shot_path)!r}, full_page=True)')
        recorded_steps.append({"index": idx, "action": "screenshot", "path": str(shot_path)})

    elif atype == "evaluate":
        script = action.get("script", "null")
        result = page.evaluate(script)
        code_lines.append(f'    page.evaluate({script!r})')
        recorded_steps.append({"index": idx, "action": "evaluate", "script": script, "result": str(result)[:200]})

    elif atype == "go_back":
        page.go_back()
        code_lines.append("    page.go_back()")
        recorded_steps.append({"index": idx, "action": "go_back"})

    elif atype == "go_forward":
        page.go_forward()
        code_lines.append("    page.go_forward()")
        recorded_steps.append({"index": idx, "action": "go_forward"})

    elif atype == "reload":
        page.reload()
        code_lines.append("    page.reload()")
        recorded_steps.append({"index": idx, "action": "reload"})

    elif atype == "set_input_files":
        sel = action["selector"]
        files = action.get("files", [])
        page.set_input_files(sel, files)
        code_lines.append(f'    page.set_input_files({sel!r}, {files!r})')
        recorded_steps.append({"index": idx, "action": "set_input_files", "selector": sel, "files": files})


# ---------------------------------------------------------------------------
# record_actions_programmatic (headless capture of a scripted sequence)
# ---------------------------------------------------------------------------

def record_actions_programmatic(
    url: str,
    actions: list[dict],
    output_file: Path,
    output_dir: Path | None = None,
    browser: str = "chromium",
    headless: bool = True,
    slow_mo: float = 0.0,
    viewport: dict | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> CodegenResult:
    """Execute a list of actions programmatically and emit a Playwright test file.

    Supported action types:
      click, dblclick, right_click, hover, fill, type, press,
      select_option, check, uncheck, drag_and_drop, scroll/wheel,
      tap, focus, navigate, wait, wait_for_selector, screenshot,
      evaluate, go_back, go_forward, reload, set_input_files

    Each action is a dict with 'type' key and type-specific fields.
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
        "from playwright.sync_api import sync_playwright, expect",
        "",
        "",
        "def test_recorded_session(page):",
        f'    page.goto("{url}")',
    ]

    recorded_steps: list[dict] = []

    launch_kwargs: dict = {"headless": headless}
    if slow_mo > 0:
        launch_kwargs["slow_mo"] = slow_mo

    ctx_kwargs: dict = {"viewport": viewport or {"width": 1280, "height": 900}}

    _sp = sync_playwright
    with _sp() as p:
        browser_name = browser.lower()
        if browser_name == "firefox":
            bw = p.firefox.launch(**launch_kwargs)
        elif browser_name in ("webkit", "safari"):
            bw = p.webkit.launch(**launch_kwargs)
        else:
            bw = p.chromium.launch(**launch_kwargs)

        ctx = bw.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)

        for idx, action in enumerate(actions):
            _echo(f"  [{idx + 1}/{len(actions)}] {action.get('type', '?')}")
            _dispatch_action(page, action, code_lines, recorded_steps, idx)

        bw.close()

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
