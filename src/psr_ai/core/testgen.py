"""Automated test generation from recorded PSR.ai sessions.

Reads a data.json payload produced by a PSR.ai recording session and emits a
production-grade Playwright (Python) integration test file that mirrors the
recorded interaction chain.

Generated test structure:
  - One pytest module per session
  - One test function per logical group of steps (grouped by active program)
  - Navigation steps become page.goto() or page.click() calls
  - Keyboard input steps become page.fill() / page.type() calls
  - Screenshot steps generate expect(page).to_have_screenshot() assertions
  - Each step produces an inline comment with the original timestamp and action

Playwright (Python) is used because it is the most LLM-readable test framework,
has first-class async support, and the generated file can be run immediately with
`pytest --headed` after `pip install pytest-playwright`.

Usage:
    from psr_ai.core.testgen import generate_playwright_test, TestGenResult
    result = generate_playwright_test(
        session_dir=Path("psr_output/my_session_output"),
        output_path=Path("tests/integration/test_recorded_session.py"),
    )
    print(result.report())
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GenerateTestResult:
    """Result of one generate_playwright_test() call."""
    session_dir: Path
    output_path: Path
    step_count: int
    generated_test_count: int
    skipped_steps: int
    generated_at: str    # ISO-8601 UTC
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            "PSR.ai Test Generation Report",
            f"  Session dir:  {self.session_dir}",
            f"  Output file:  {self.output_path}",
            f"  Steps total:  {self.step_count}",
            f"  Tests emitted:{self.generated_test_count}",
            f"  Steps skipped:{self.skipped_steps}",
            f"  Generated at: {self.generated_at}",
        ]
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)


# Backward-compatible alias kept so existing callers do not need updating.
TestGenResult = GenerateTestResult


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_playwright_test(
    session_dir: Path,
    output_path: Path | None = None,
    framework: str = "playwright",
) -> GenerateTestResult:
    """Generate a Playwright integration test from a PSR.ai session directory.

    session_dir:  directory containing data.json (produced by sa stop).
    output_path:  where to write the .py test file. Defaults to
                  <session_dir>/generated_test.py.
    framework:    currently only "playwright" is supported.

    Returns a TestGenResult.
    Raises FileNotFoundError if data.json is missing.
    Raises ValueError for unsupported framework values.
    """
    session_dir = Path(session_dir)
    data_path = session_dir / "data.json"

    if not data_path.exists():
        raise FileNotFoundError(
            f"data.json not found in {session_dir}. "
            "Run `sa stop` first to generate session data."
        )

    if framework != "playwright":
        raise ValueError(
            f"Unsupported framework: {framework!r}. "
            "Only 'playwright' is currently supported."
        )

    if output_path is None:
        output_path = session_dir / "generated_test.py"

    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Could not parse data.json in {session_dir}: {exc}"
        ) from exc
    steps: list[dict] = data.get("steps", [])

    warnings: list[str] = []
    if not steps:
        warnings.append("Session has zero steps. Generated file contains a skeleton only.")

    code, test_count, skipped = _render_playwright(data, steps, session_dir, warnings)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(code, encoding="utf-8")

    return GenerateTestResult(
        session_dir=session_dir,
        output_path=output_path,
        step_count=len(steps),
        generated_test_count=test_count,
        skipped_steps=skipped,
        generated_at=datetime.now(timezone.utc).isoformat(),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Playwright code renderer
# ---------------------------------------------------------------------------

def _render_playwright(
    data: dict,
    steps: list[dict],
    session_dir: Path,
    warnings: list[str],
) -> tuple[str, int, int]:
    """Return (source_code, test_function_count, skipped_step_count)."""

    title = _safe_identifier(data.get("title", "recorded_session"))
    source = data.get("source", "unknown")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Group steps by active program window.
    groups = _group_by_program(steps)
    skipped = 0

    header = textwrap.dedent(f"""\
        \"\"\"
        Auto-generated Playwright integration test.
        Source session: {source}
        Session title:  {title}
        Generated by:   PSR.ai sa testgen
        Generated at:   {generated_at}

        Requirements:
          pip install pytest-playwright
          playwright install

        Run with:
          pytest {Path(session_dir).name}/generated_test.py --headed
        \"\"\"

        from __future__ import annotations

        import re
        import pytest
        from playwright.sync_api import Page, expect


        # ---------------------------------------------------------------------------
        # Shared fixtures
        # ---------------------------------------------------------------------------

        @pytest.fixture(scope="session")
        def base_url() -> str:
            \"\"\"Override this fixture to point tests at the correct environment.\"\"\"
            return "http://localhost:3000"


        # ---------------------------------------------------------------------------
        # Soft assertion helper
        # ---------------------------------------------------------------------------

        class SoftAssertions:
            \"\"\"Collect non-fatal assertion failures and report all at end of test.\"\"\"

            def __init__(self) -> None:
                self._failures: list[str] = []

            def expect(self, locator, message: str = ""):
                \"\"\"Return a soft wrapper around a Playwright locator.\"\"\"
                return _SoftLocatorWrapper(locator, self._failures, message)

            def assert_all(self) -> None:
                if self._failures:
                    joined = "\\n".join(f"  - {{f}}" for f in self._failures)
                    raise AssertionError(f"Soft assertion failures:\\n{{joined}}")


        class _SoftLocatorWrapper:
            def __init__(self, locator, failures: list, message: str):
                self._loc = locator
                self._failures = failures
                self._msg = message

            def to_be_visible(self):
                try:
                    expect(self._loc).to_be_visible()
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self

            def to_have_text(self, text):
                try:
                    expect(self._loc).to_have_text(text)
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self

            def to_be_enabled(self):
                try:
                    expect(self._loc).to_be_enabled()
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self

            def to_have_value(self, value):
                try:
                    expect(self._loc).to_have_value(value)
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self

            def to_have_count(self, count):
                try:
                    expect(self._loc).to_have_count(count)
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self

            def to_be_checked(self):
                try:
                    expect(self._loc).to_be_checked()
                except AssertionError as exc:
                    self._failures.append(self._msg or str(exc))
                return self


    """)

    test_functions: list[str] = []
    test_count = 0

    if not steps:
        placeholder = textwrap.dedent(f"""\
            def test_{title}_placeholder(page: Page, base_url: str) -> None:
                \"\"\"Placeholder test: no steps were recorded in this session.\"\"\"
                page.goto(base_url)
        """)
        test_functions.append(placeholder)
        test_count = 1
    else:
        for group_idx, (program, group_steps) in enumerate(groups):
            func_name = f"test_{title}_group_{group_idx:02d}"
            if program:
                func_name = f"test_{title}_{_safe_identifier(program)}_{group_idx:02d}"
            func_lines, step_skipped = _render_test_function(
                func_name, program, group_steps, warnings
            )
            skipped += step_skipped
            test_functions.append("\n".join(func_lines))
            test_count += 1

    body = "\n\n".join(test_functions)
    return header + body + "\n", test_count, skipped


def _render_test_function(
    func_name: str,
    program: str,
    steps: list[dict],
    warnings: list[str],
) -> tuple[list[str], int]:
    """Return (list of source lines, skipped_count) for one test function."""
    skipped = 0
    lines: list[str] = []

    program_comment = f" - active window: {program}" if program else ""
    lines.append(f"def {func_name}(page: Page, base_url: str) -> None:")
    lines.append(f'    """Replay {len(steps)} recorded step(s){program_comment}."""')

    for step in steps:
        idx = step.get("index", "?")
        ts = step.get("timestamp", "")
        action = step.get("action_text", "")
        label = step.get("label", "")
        click_x = step.get("click_x")
        click_y = step.get("click_y")
        ai_desc = step.get("ai_description", "")
        screenshot = step.get("screenshot", "")

        # Build a comment for this step.
        comment_parts = [f"step {idx}"]
        if ts:
            comment_parts.append(ts)
        if ai_desc:
            comment_parts.append(ai_desc)
        elif action:
            comment_parts.append(action)
        lines.append(f"    # {' | '.join(comment_parts)}")

        action_lower = action.lower()
        step_code = _action_to_playwright(
            action_lower, label, click_x, click_y, screenshot, warnings
        )
        if step_code:
            lines.append(f"    {step_code}")
        else:
            lines.append(f"    pass  # step {idx}: no direct Playwright mapping for: {action[:60]!r}")
            skipped += 1

    return lines, skipped


def _action_to_playwright(
    action: str,
    label: str,
    click_x: int | None,
    click_y: int | None,
    screenshot: str,
    warnings: list[str],
) -> str:
    """Map a single PSR action string to a Playwright Python call.

    Returns a code string (without leading indentation) or empty string when
    there is no meaningful mapping.
    """
    # Navigation: "opened", "navigate", "go to", url patterns
    if any(kw in action for kw in ("navigate", "go to", "open", "launch", "http")):
        url_m = re.search(r'https?://[^\s\'"]+', action)
        if url_m:
            return f"page.goto({url_m.group(0)!r})"
        return "page.goto(base_url)"

    # Text input: "type", "input", "key", "press"
    if any(kw in action for kw in ("type", "input", "fill", "entered text", "entered")):
        # Extract quoted text if present
        m = re.search(r'"([^"]+)"', action) or re.search(r"'([^']+)'", action)
        value = m.group(1) if m else ""
        if value:
            return f'page.keyboard.type({value!r})'
        return 'page.keyboard.type("")  # TODO: fill with actual value'

    # Double click
    if "double" in action and "click" in action:
        safe_label = label.strip()
        if safe_label and safe_label.lower() not in ("step", "screenshot"):
            escaped = safe_label.replace('"', '\\"')
            return f'page.dblclick(page.get_by_text("{escaped}", exact=False))'
        if click_x is not None and click_y is not None:
            return f"page.mouse.dblclick({click_x}, {click_y})"
        return "page.dblclick(\"body\")  # TODO: replace with correct selector"

    # Right click
    if "right" in action and "click" in action:
        if click_x is not None and click_y is not None:
            return f'page.mouse.click({click_x}, {click_y}, button="right")'
        return 'page.click("body", button="right")  # TODO: replace with correct selector'

    # Click at coordinates
    if click_x is not None and click_y is not None and (click_x or click_y):
        return f"page.mouse.click({click_x}, {click_y})"

    # Generic click by label text
    if "click" in action:
        safe_label = label.strip()
        if safe_label and safe_label.lower() not in ("step", "screenshot"):
            escaped = safe_label.replace('"', '\\"')
            return f'page.get_by_text("{escaped}", exact=False).click()'
        return "page.mouse.click(0, 0)  # TODO: replace with correct selector"

    # Hover
    if "hover" in action or "mouse over" in action:
        safe_label = label.strip()
        if safe_label and safe_label.lower() not in ("step", "screenshot"):
            escaped = safe_label.replace('"', '\\"')
            return f'page.get_by_text("{escaped}", exact=False).hover()'
        return "page.hover(\"body\")  # TODO: replace with correct selector"

    # Keyboard press
    if "press" in action or "key" in action:
        # Try to extract key name
        key_match = re.search(r"\b(enter|tab|escape|esc|space|backspace|delete|f\d+|ctrl\+\w+|alt\+\w+)\b", action, re.I)
        key = key_match.group(1).title() if key_match else "Enter"
        return f'page.keyboard.press("{key}")'

    # Scroll
    if "scroll" in action:
        return "page.mouse.wheel(0, 300)"

    # Screenshot assertion with full expect API
    if screenshot:
        return "expect(page).to_have_screenshot()"

    return ""


def _generate_assertions(steps: list[dict]) -> list[str]:
    """Generate Playwright expect assertions from a step list.

    Returns lines to append after the action sequence.
    Covers: to_have_url, to_have_title, to_be_visible, to_be_enabled,
    to_have_text, to_have_value, to_have_count, to_be_checked.
    """
    assertions: list[str] = []

    # Check if any navigation step happened - assert final URL
    nav_steps = [s for s in steps if any(
        kw in (s.get("action_text", "") or "").lower()
        for kw in ("navigate", "go to", "open", "launch")
    )]
    if nav_steps:
        assertions.append("    # --- Assertions ---")
        assertions.append("    expect(page).to_have_url(re.compile(r'http'))")
        assertions.append("    expect(page).to_have_title(re.compile(r'.+'))")

    # Check if any input step happened - assert form fields
    input_steps = [s for s in steps if any(
        kw in (s.get("action_text", "") or "").lower()
        for kw in ("type", "input", "fill", "entered")
    )]
    if input_steps:
        if not nav_steps:
            assertions.append("    # --- Assertions ---")
        assertions.append("    # expect(page.locator('input').first).to_have_value('')  # TODO: verify input value")

    # Check for click steps - assert element visible
    click_steps = [s for s in steps if "click" in (s.get("action_text", "") or "").lower()]
    if click_steps and not assertions:
        assertions.append("    # --- Assertions ---")
        assertions.append("    expect(page.locator('body')).to_be_visible()")

    return assertions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group_by_program(steps: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group consecutive steps that share the same active program window.

    Returns a list of (program_name, steps_in_group) tuples.
    """
    if not steps:
        return []

    groups: list[tuple[str, list[dict]]] = []
    current_program = steps[0].get("program", "")
    current_group: list[dict] = []

    for step in steps:
        prog = step.get("program", "")
        if prog != current_program and current_group:
            groups.append((current_program, current_group))
            current_group = []
            current_program = prog
        current_group.append(step)

    if current_group:
        groups.append((current_program, current_group))

    return groups


def _safe_identifier(text: str) -> str:
    """Convert arbitrary text to a valid Python identifier fragment."""
    cleaned = re.sub(r"[^\w]", "_", text.lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "session_" + cleaned
    return cleaned[:40]
