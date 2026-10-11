"""Unit tests for core/generator.py - MarkdownGenerator and helper functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psr_ai.core.generator import (
    MarkdownGenerator,
    _safe_relative,
    _to_anchor,
    generate_markdown,
)
from psr_ai.core.parser import RecordingSession, Step


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


def _make_data_json(
    tmp_path: Path,
    steps: list[dict] | None = None,
    title: str = "Test Session",
    source: str = "test.zip",
) -> Path:
    """Write a minimal data.json and return its path."""
    if steps is None:
        steps = [
            {
                "index": 0,
                "label": "Click button",
                "timestamp": "1/15/2024 14:30:00 PM",
                "ui_element": "button#submit",
                "program": "Chrome",
                "action_text": "Left-click on Submit",
                "screenshot": "screenshots/step_000.png",
            },
            {
                "index": 1,
                "label": "Type text",
                "timestamp": "1/15/2024 14:30:05 PM",
                "ui_element": "input#search",
                "program": "Chrome",
                "action_text": "Type: hello world",
                "screenshot": "screenshots/step_001.png",
            },
        ]
    payload = {
        "title": title,
        "source": source,
        "generated_at": "2024-01-15T14:30:00+00:00",
        "step_count": len(steps),
        "steps": steps,
    }
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return data_path


def _make_session(tmp_path: Path, step_count: int = 2) -> RecordingSession:
    """Build a minimal RecordingSession with synthetic steps."""
    steps = [
        Step(
            index=i,
            label=f"Click step {i}",
            image_bytes=_TINY_PNG,
            timestamp=f"1/15/2024 14:30:0{i} PM",
            program="Chrome",
            action_text=f"Action {i}",
        )
        for i in range(step_count)
    ]
    return RecordingSession(
        source_zip=tmp_path / "test.zip",
        steps=steps,
        title="Test Session",
    )


# ---------------------------------------------------------------------------
# MarkdownGenerator.write
# ---------------------------------------------------------------------------

class TestMarkdownGeneratorWrite:

    def test_creates_report_md(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        gen = MarkdownGenerator(data_path)
        report = gen.write(tmp_path)

        assert report.name == "report.md"
        assert report.exists()

    def test_report_is_utf8(self, tmp_path):
        data_path = _make_data_json(tmp_path, title="Session - test: ok")
        gen = MarkdownGenerator(data_path)
        report = gen.write(tmp_path)

        content = report.read_text(encoding="utf-8")
        assert "Session - test: ok" in content

    def test_report_contains_title(self, tmp_path):
        data_path = _make_data_json(tmp_path, title="My Workflow")
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "# My Workflow" in content

    def test_report_contains_source_name(self, tmp_path):
        data_path = _make_data_json(tmp_path, source="recording_20240115.zip")
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "recording_20240115.zip" in content

    def test_report_contains_step_count(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        # Step count appears in header and footer.
        assert "2" in content

    def test_creates_output_dir_if_missing(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        out_dir = tmp_path / "nested" / "output"
        report = MarkdownGenerator(data_path).write(out_dir)

        assert out_dir.exists()
        assert report.exists()


# ---------------------------------------------------------------------------
# AI Context Block
# ---------------------------------------------------------------------------

class TestAIContextBlock:

    def test_ai_block_present(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "## AI Context Block" in content

    def test_ai_block_contains_step_lines(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "[000]" in content
        assert "[001]" in content

    def test_ai_block_contains_program_name(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "Chrome" in content

    def test_ai_block_contains_action_text(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "Left-click on Submit" in content

    def test_ai_block_fenced_in_code_block(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        # Must be wrapped in a code fence.
        assert "```text" in content

    def test_ai_block_screenshot_flag(self, tmp_path):
        # Steps with screenshots should show their screenshot path.
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "-> screenshots/step_000.png" in content

    def test_ai_block_no_screenshot_flag(self, tmp_path):
        steps = [{"index": 0, "label": "Step 0", "timestamp": "",
                  "ui_element": "", "program": "", "action_text": "Action", "screenshot": ""}]
        data_path = _make_data_json(tmp_path, steps=steps)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "-> -" in content

    def test_ai_block_empty_session(self, tmp_path):
        data_path = _make_data_json(tmp_path, steps=[])
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "## AI Context Block" in content
        assert "No steps recorded" in content

    def test_ai_block_no_em_dashes(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        # Em dash (U+2014) and en dash (U+2013) must never appear.
        assert "—" not in content
        assert "–" not in content


# ---------------------------------------------------------------------------
# Timeline rendering
# ---------------------------------------------------------------------------

class TestTimelineRendering:

    def test_timeline_header_present(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "## Step-by-Step Timeline" in content

    def test_step_label_in_heading(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "### Step 0: Click button" in content
        assert "### Step 1: Type text" in content

    def test_timestamp_in_table(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "1/15/2024 14:30:00 PM" in content

    def test_program_in_table(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "Chrome" in content

    def test_action_text_in_table(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "Left-click on Submit" in content

    def test_screenshot_image_link_present(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "![Step 0 screenshot](screenshots/step_000.png)" in content

    def test_no_screenshot_placeholder_when_missing(self, tmp_path):
        steps = [{"index": 0, "label": "A", "timestamp": "", "ui_element": "",
                  "program": "", "action_text": "action", "screenshot": ""}]
        data_path = _make_data_json(tmp_path, steps=steps)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "_No screenshot captured for this step._" in content

    def test_empty_session_placeholder(self, tmp_path):
        data_path = _make_data_json(tmp_path, steps=[])
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "_No steps were recorded in this session._" in content


# ---------------------------------------------------------------------------
# Table of Contents
# ---------------------------------------------------------------------------

class TestTableOfContents:

    def test_toc_present(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "## Table of Contents" in content

    def test_toc_has_entry_per_step(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "- [Step 0: Click button]" in content
        assert "- [Step 1: Type text]" in content

    def test_toc_absent_for_empty_session(self, tmp_path):
        data_path = _make_data_json(tmp_path, steps=[])
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "## Table of Contents" not in content


# ---------------------------------------------------------------------------
# Screenshot path safety
# ---------------------------------------------------------------------------

class TestScreenshotPathSafety:

    def test_normal_relative_path_preserved(self):
        assert _safe_relative("screenshots/step_000.png") == "screenshots/step_000.png"

    def test_backslashes_converted_to_forward_slashes(self):
        result = _safe_relative("screenshots\\step_000.png")
        assert "\\" not in result
        assert "screenshots/step_000.png" in result

    def test_path_traversal_stripped(self):
        assert _safe_relative("../../../etc/passwd") == ""

    def test_leading_slash_stripped(self):
        result = _safe_relative("/screenshots/step_000.png")
        assert not result.startswith("/")

    def test_double_dot_in_middle_collapsed(self):
        result = _safe_relative("screenshots/../step_000.png")
        assert ".." not in result

    def test_path_traversal_in_report_renders_empty(self, tmp_path):
        # A crafted screenshot path in data.json must not appear in the report.
        steps = [{"index": 0, "label": "X", "timestamp": "", "ui_element": "",
                  "program": "", "action_text": "x",
                  "screenshot": "../../secret.txt"}]
        data_path = _make_data_json(tmp_path, steps=steps)
        report = MarkdownGenerator(data_path).write(tmp_path)
        content = report.read_text(encoding="utf-8")

        assert "secret.txt" not in content
        assert "_No screenshot captured for this step._" in content


# ---------------------------------------------------------------------------
# Anchor slug helper
# ---------------------------------------------------------------------------

class TestToAnchor:

    def test_lowercases(self):
        assert _to_anchor("Hello World") == "hello-world"

    def test_spaces_become_hyphens(self):
        assert _to_anchor("step 0 click") == "step-0-click"

    def test_strips_special_chars(self):
        result = _to_anchor("step-0-click!")
        assert "!" not in result

    def test_colons_stripped(self):
        result = _to_anchor("step:0")
        assert ":" not in result


# ---------------------------------------------------------------------------
# generate_markdown (legacy API / sa pub path)
# ---------------------------------------------------------------------------

class TestGenerateMarkdown:

    def test_returns_report_md(self, tmp_path):
        session = _make_session(tmp_path)
        result = generate_markdown(session, tmp_path / "output")

        assert result.name == "report.md"
        assert result.exists()

    def test_screenshots_written(self, tmp_path):
        session = _make_session(tmp_path, step_count=2)
        out_dir = tmp_path / "output"
        generate_markdown(session, out_dir)

        assert (out_dir / "screenshots" / "step_000.png").exists()
        assert (out_dir / "screenshots" / "step_001.png").exists()

    def test_data_json_written(self, tmp_path):
        session = _make_session(tmp_path)
        out_dir = tmp_path / "output"
        generate_markdown(session, out_dir)

        data_path = out_dir / "data.json"
        assert data_path.exists()
        data = json.loads(data_path.read_text(encoding="utf-8"))
        assert data["title"] == "Test Session"

    def test_report_references_screenshots(self, tmp_path):
        session = _make_session(tmp_path, step_count=2)
        out_dir = tmp_path / "output"
        report = generate_markdown(session, out_dir)
        content = report.read_text(encoding="utf-8")

        assert "screenshots/step_000.png" in content

    def test_no_em_dashes_in_output(self, tmp_path):
        session = _make_session(tmp_path)
        report = generate_markdown(session, tmp_path / "output")
        content = report.read_text(encoding="utf-8")

        assert "—" not in content
        assert "–" not in content
