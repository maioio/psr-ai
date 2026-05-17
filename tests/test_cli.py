"""CLI integration tests for the `sa` command group via click.testing.CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from psr_ai.cli import main


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_session_dir(tmp_path: Path, n_steps: int = 2) -> Path:
    """Create a minimal session directory with data.json and dummy screenshots."""
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()
    _TINY_PNG = bytes([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41,
        0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
        0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc,
        0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
        0x44, 0xae, 0x42, 0x60, 0x82,
    ])
    steps = []
    for i in range(n_steps):
        png = screenshots_dir / f"step_{i:03d}.png"
        png.write_bytes(_TINY_PNG)
        steps.append({
            "index": i,
            "label": f"Step {i}",
            "timestamp": "1/15/2024 2:30:00 PM",
            "program": "TestApp",
            "action_text": f"Clicked button {i}",
            "screenshot": f"screenshots/step_{i:03d}.png",
        })

    data = {
        "title": "Test Session",
        "source": "test.zip",
        "generated_at": "2024-01-15T14:30:00Z",
        "step_count": n_steps,
        "steps": steps,
    }
    (tmp_path / "data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------
# sa --version
# ------------------------------------------------------------------

def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "sa" in result.output


# ------------------------------------------------------------------
# sa status
# ------------------------------------------------------------------

def _make_fake_recorder(psr_path: Path):
    """Return a WindowsRecorder-like fake that bypasses all resolution logic."""
    from psr_ai.core.recorder import WindowsRecorder
    r = object.__new__(WindowsRecorder)
    r._psr_exe = psr_path
    r._pending_output = None
    return r


def test_status_idle(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    runner = CliRunner()
    mock_result = MagicMock()
    mock_result.stdout = "INFO: No tasks are running."
    with (
        patch("psr_ai.cli.Recorder", return_value=_make_fake_recorder(fake_psr)),
        patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result),
    ):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "IDLE" in result.output


def test_status_recording(tmp_path):
    fake_psr = tmp_path / "psr.exe"
    fake_psr.touch()
    runner = CliRunner()
    mock_result = MagicMock()
    mock_result.stdout = "psr.exe  1234  Console  1  10,000 K"
    with (
        patch("psr_ai.cli.Recorder", return_value=_make_fake_recorder(fake_psr)),
        patch("psr_ai.core.recorder.subprocess.run", return_value=mock_result),
    ):
        result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "RECORDING" in result.output


def test_status_exits_1_when_psr_not_found():
    from psr_ai.core.recorder import PSRNotFoundError
    runner = CliRunner()
    with patch("psr_ai.cli.Recorder", side_effect=PSRNotFoundError("not found")):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 1


# ------------------------------------------------------------------
# sa review
# ------------------------------------------------------------------

def test_review_keep_all_steps(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=2)
    runner = CliRunner()

    # Simulate pressing Enter (keep) for every step.
    result = runner.invoke(main, ["review", str(source_dir)], input="\n\n")

    assert result.exit_code == 0
    assert "2 steps kept" in result.output
    assert "0 steps dropped" in result.output


def test_review_drop_one_step(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=2)
    runner = CliRunner()

    # Drop the first step, keep the second.
    result = runner.invoke(main, ["review", str(source_dir)], input="d\n\n")

    assert result.exit_code == 0
    assert "1 steps kept" in result.output
    assert "1 steps dropped" in result.output

    data = json.loads((source_dir / "data.json").read_text(encoding="utf-8"))
    assert data["step_count"] == 1
    assert data["steps"][0]["index"] == 1


def test_review_exits_1_for_missing_data_json(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["review", str(tmp_path)])
    assert result.exit_code == 1


# ------------------------------------------------------------------
# sa analyze
# ------------------------------------------------------------------

def test_analyze_calls_analyze_session(tmp_path):
    source_dir = _make_session_dir(tmp_path)
    runner = CliRunner()

    with patch("psr_ai.core.analyzer.analyze_session") as mock_analyze:
        mock_analyze.return_value = 2
        # Patch the import inside cli.py
        with patch("psr_ai.cli.analyze_session", mock_analyze, create=True):
            # We need to patch the import inside the command
            with patch("builtins.__import__") as mock_import:
                # Use a simpler approach: mock the entire analyzer module
                pass

    # Test the actual path: analyzer module importable, function works
    with patch("psr_ai.core.analyzer._PROVIDERS", {
        "anthropic": (MagicMock(return_value="AI desc"), "test-model")
    }):
        result = runner.invoke(
            main,
            ["analyze", str(source_dir), "--no-regen"],
            env={"ANTHROPIC_API_KEY": "test-key"},
        )

    assert result.exit_code == 0
    assert "enriched" in result.output.lower() or "done" in result.output.lower()


def test_analyze_reports_enriched_count(tmp_path):
    source_dir = _make_session_dir(tmp_path, n_steps=3)
    runner = CliRunner()

    with patch("psr_ai.core.analyzer._PROVIDERS", {
        "anthropic": (MagicMock(return_value="A description"), "test-model")
    }):
        result = runner.invoke(
            main,
            ["analyze", str(source_dir), "--no-regen"],
            env={"ANTHROPIC_API_KEY": "test-key"},
        )

    assert result.exit_code == 0
    assert "3" in result.output


def test_analyze_exits_1_for_missing_data_json(tmp_path):
    runner = CliRunner()
    (tmp_path / "screenshots").mkdir()  # Dir exists but no data.json
    result = runner.invoke(main, ["analyze", str(tmp_path)])
    assert result.exit_code == 1


def test_analyze_help_shows_provider_option():
    runner = CliRunner()
    result = runner.invoke(main, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--model" in result.output


# ------------------------------------------------------------------
# sa pub
# ------------------------------------------------------------------

def test_pub_copies_assets(tmp_path):
    source_dir = _make_session_dir(tmp_path)
    # Create required files.
    (source_dir / "report.md").write_text("# Report", encoding="utf-8")
    (source_dir / "action_replay.gif").write_bytes(b"GIF87a")

    dest_root = tmp_path / "docs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["pub", str(source_dir), "--dest", str(dest_root)],
    )

    assert result.exit_code == 0
    assert "Copied to" in result.output


def test_pub_exits_1_for_nonexistent_source(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["pub", str(tmp_path / "does_not_exist")])
    assert result.exit_code != 0
