"""Self-healing code pipeline.

Reads the latest PSR.ai session data to pin failure context, runs the test
suite, extracts failing test information, then reports actionable diagnostics.

The healer is deliberately conservative: it diagnoses and reports, it does not
rewrite source files autonomously. Autonomous source rewrites require human
confirmation to avoid silent regressions. The `heal` function returns a
structured `HealResult` that callers (CLI, CI scripts, or an LLM agent) use to
decide the next action.

Typical programmatic usage:
    from psr_ai.core.healer import heal, HealResult
    result = heal(session_dir=Path("psr_output/session_output"), project_root=Path("."))
    if not result.all_green:
        print(result.report())
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FailingTest:
    """One failing test case extracted from pytest output."""
    nodeid: str          # e.g. "tests/test_parser.py::test_extract_zip_valid"
    short_summary: str   # the AssertionError / exception line
    source_file: str     # best-guess source file path (may be empty)
    source_hint: str     # context text pointing to the likely code location


@dataclass
class HealResult:
    """Result of one heal() invocation."""
    session_dir: Path | None
    project_root: Path
    ran_at: str                              # ISO-8601 UTC
    returncode: int                          # pytest exit code
    total_collected: int
    passed: int
    failed: int
    errors: int
    failing_tests: list[FailingTest] = field(default_factory=list)
    session_context: dict = field(default_factory=dict)  # extracted from data.json

    @property
    def all_green(self) -> bool:
        return self.returncode == 0

    def report(self) -> str:
        """Return a plain-text diagnostic report for display or LLM ingestion."""
        lines: list[str] = [
            "PSR.ai Self-Heal Report",
            f"  Ran at:       {self.ran_at}",
            f"  Project root: {self.project_root}",
            f"  Result:       {'GREEN' if self.all_green else 'FAILING'}",
            f"  Collected:    {self.total_collected}",
            f"  Passed:       {self.passed}",
            f"  Failed:       {self.failed}",
            f"  Errors:       {self.errors}",
        ]

        if self.session_context:
            lines.append("")
            lines.append("Session context (from latest PSR.ai recording):")
            for key, val in self.session_context.items():
                lines.append(f"  {key}: {val}")

        if self.failing_tests:
            lines.append("")
            lines.append("Failing tests:")
            for ft in self.failing_tests:
                lines.append(f"  [{ft.nodeid}]")
                if ft.source_file:
                    lines.append(f"    Source: {ft.source_file}")
                if ft.short_summary:
                    lines.append(f"    Error:  {ft.short_summary}")
                if ft.source_hint:
                    lines.append(f"    Hint:   {ft.source_hint}")

        if self.all_green:
            lines.append("")
            lines.append("All tests pass. No action required.")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def heal(
    project_root: Path,
    session_dir: Path | None = None,
    pytest_args: Sequence[str] = (),
    max_failures: int = 20,
) -> HealResult:
    """Run the test suite and return a structured diagnostic result.

    project_root: the root directory of the project (where pyproject.toml lives).
    session_dir:  optional path to a PSR.ai session output directory. When
                  supplied, data.json is read for context: the last recorded
                  step's timestamp, program, and action are included in the
                  report to correlate a crash with the user activity that
                  preceded it.
    pytest_args:  additional arguments forwarded to pytest (e.g. ["-x", "-k", "parser"]).
    max_failures: cap the number of failing test entries to keep in the result.

    Returns a HealResult. Never raises.
    """
    project_root = Path(project_root).resolve()
    ran_at = datetime.now(timezone.utc).isoformat()

    session_context = _load_session_context(session_dir) if session_dir else {}

    raw_output, returncode = _run_pytest(project_root, pytest_args)

    totals = _parse_totals(raw_output)
    failing = _parse_failures(raw_output, project_root)[:max_failures]

    return HealResult(
        session_dir=session_dir,
        project_root=project_root,
        ran_at=ran_at,
        returncode=returncode,
        total_collected=totals["collected"],
        passed=totals["passed"],
        failed=totals["failed"],
        errors=totals["errors"],
        failing_tests=failing,
        session_context=session_context,
    )


# ---------------------------------------------------------------------------
# Session context extraction
# ---------------------------------------------------------------------------

def _load_session_context(session_dir: Path) -> dict:
    """Read data.json from a PSR session directory and extract failure context.

    Returns a flat dict with the most recent step's timestamp, program, and
    action text. Returns an empty dict if data.json is absent or unreadable.
    """
    data_path = Path(session_dir) / "data.json"
    if not data_path.exists():
        return {}
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    steps: list[dict] = data.get("steps", [])
    if not steps:
        return {
            "session_title": data.get("title", ""),
            "session_source": data.get("source", ""),
            "step_count": 0,
            "last_step_timestamp": "",
            "last_step_program": "",
            "last_step_action": "",
        }

    last = steps[-1]
    return {
        "session_title": data.get("title", ""),
        "session_source": data.get("source", ""),
        "step_count": len(steps),
        "last_step_timestamp": last.get("timestamp", ""),
        "last_step_program": last.get("program", ""),
        "last_step_action": last.get("action_text", ""),
    }


# ---------------------------------------------------------------------------
# Pytest runner
# ---------------------------------------------------------------------------

def _run_pytest(project_root: Path, extra_args: Sequence[str]) -> tuple[str, int]:
    """Run pytest with JSON-friendly output and return (stdout+stderr, exit code)."""
    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short",
        "--no-header",
        "-q",
        *extra_args,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        return result.stdout + result.stderr, result.returncode
    except (FileNotFoundError, OSError) as exc:
        return f"pytest could not be launched: {exc}", 1
    except subprocess.TimeoutExpired:
        return "pytest timed out after 300 seconds.", 1


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

# Matches lines like: "3 failed, 12 passed, 1 error in 0.45s"
_RE_SUMMARY = re.compile(
    r'(?:(\d+)\s+failed)?.*?(?:(\d+)\s+passed)?.*?(?:(\d+)\s+error)?',
    re.IGNORECASE,
)

# Matches "collected N items" or "N/N"
_RE_COLLECTED = re.compile(r'collected\s+(\d+)\s+item', re.IGNORECASE)

# Matches FAILED test node IDs like "FAILED tests/test_x.py::test_y - AssertionError: ..."
_RE_FAILED_LINE = re.compile(r'^FAILED\s+(.+?)(?:\s+-\s+(.+))?$', re.MULTILINE)

# Matches "short test summary info" section entries
_RE_SHORT_SUMMARY = re.compile(r'^FAILED\s+([\w/\\.:]+)', re.MULTILINE)

# Matches Python source file references in tracebacks: "tests/foo.py:42: in test_bar"
_RE_TRACEBACK_FILE = re.compile(r'^([\w/\\.\-]+\.py):(\d+):', re.MULTILINE)


def _parse_totals(output: str) -> dict:
    """Extract pass/fail/error counts from pytest stdout."""
    collected = 0
    m = _RE_COLLECTED.search(output)
    if m:
        collected = int(m.group(1))

    passed = failed = errors = 0

    # Look for the summary line at the bottom of pytest output.
    # Format: "X failed, Y passed, Z error in N.Ns"
    for line in reversed(output.splitlines()):
        line = line.strip()
        if "passed" in line or "failed" in line or "error" in line:
            failed_m = re.search(r'(\d+)\s+failed', line)
            passed_m = re.search(r'(\d+)\s+passed', line)
            error_m  = re.search(r'(\d+)\s+error', line)
            if failed_m:
                failed = int(failed_m.group(1))
            if passed_m:
                passed = int(passed_m.group(1))
            if error_m:
                errors = int(error_m.group(1))
            if collected == 0:
                collected = passed + failed + errors
            break

    return {"collected": collected, "passed": passed, "failed": failed, "errors": errors}


def _parse_failures(output: str, project_root: Path) -> list[FailingTest]:
    """Extract structured failure information from pytest output."""
    failures: list[FailingTest] = []

    for m in _RE_FAILED_LINE.finditer(output):
        nodeid = m.group(1).strip()
        summary = (m.group(2) or "").strip()

        # Guess the source file from the nodeid (everything before "::")
        source_file = ""
        if "::" in nodeid:
            candidate = nodeid.split("::")[0]
            candidate_path = project_root / candidate
            if candidate_path.exists():
                source_file = str(candidate_path)

        # Extract a source hint from the traceback - the last non-test file referenced.
        hint = _extract_source_hint(output, nodeid, project_root)

        failures.append(FailingTest(
            nodeid=nodeid,
            short_summary=summary[:200],
            source_file=source_file,
            source_hint=hint,
        ))

    return failures


def _extract_source_hint(output: str, nodeid: str, project_root: Path) -> str:
    """Find the last src/ traceback line associated with a test node ID.

    Looks for file:line references that point into the src/ tree (not tests/).
    Returns a short string like "src/psr_ai/core/parser.py:42" or empty string.
    """
    # Isolate the section of output for this test by finding its FAILED marker.
    start = output.find(nodeid)
    if start == -1:
        return ""
    section = output[max(0, start - 2000):start + 500]

    src_refs: list[str] = []
    for m in _RE_TRACEBACK_FILE.finditer(section):
        path_str = m.group(1).replace("\\", "/")
        lineno = m.group(2)
        if "src/" in path_str or "psr_ai" in path_str:
            src_refs.append(f"{path_str}:{lineno}")

    return src_refs[-1] if src_refs else ""
