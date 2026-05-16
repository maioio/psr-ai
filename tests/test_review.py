"""Unit tests for core/reviewer.py - sa review privacy workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psr_ai.core.reviewer import run_review_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)


def _make_session_dir(tmp_path: Path, step_count: int = 3) -> Path:
    """Create a minimal session directory with data.json and PNG screenshots."""
    session = tmp_path / "session_output"
    session.mkdir()

    screenshots = session / "screenshots"
    screenshots.mkdir()

    steps = []
    for i in range(step_count):
        png_name = f"step_{i:03d}.png"
        (screenshots / png_name).write_bytes(_TINY_PNG)
        steps.append({
            "index": i,
            "label": f"Step {i}",
            "timestamp": f"1/15/2024 2:30:0{i} PM",
            "ui_element": "",
            "program": "Chrome",
            "action_text": f"Action {i}",
            "screenshot": f"screenshots/{png_name}",
        })

    data = {
        "title": "Test Session",
        "source": "test.zip",
        "generated_at": "2024-01-15T00:00:00+00:00",
        "step_count": step_count,
        "steps": steps,
    }
    (session / "data.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return session


# ---------------------------------------------------------------------------
# run_review_session - core loop
# ---------------------------------------------------------------------------

class TestRunReviewSession:

    def test_keep_all_steps_returns_correct_counts(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        responses = iter(["k", "k", "k"])
        output_lines = []

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=output_lines.append,
            regenerate=False,
        )

        assert kept == 3
        assert dropped == 0

    def test_drop_all_steps_returns_correct_counts(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        responses = iter(["d", "d", "d"])

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert kept == 0
        assert dropped == 3

    def test_mixed_keep_and_drop(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        # keep, drop, keep
        responses = iter(["k", "d", ""])

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert kept == 2
        assert dropped == 1

    def test_enter_is_treated_as_keep(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=2)
        responses = iter(["", ""])  # bare Enter

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert kept == 2
        assert dropped == 0

    def test_uppercase_D_treated_as_drop(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=1)

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: "D",
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert dropped == 1

    def test_any_non_d_response_is_keep(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=1)

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: "yes",
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert kept == 1
        assert dropped == 0


# ---------------------------------------------------------------------------
# run_review_session - file mutation
# ---------------------------------------------------------------------------

class TestReviewFileMutation:

    def test_dropped_step_removed_from_data_json(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        # drop step 1, keep 0 and 2
        responses = iter(["k", "d", "k"])

        run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        data = json.loads((session / "data.json").read_text(encoding="utf-8"))
        assert data["step_count"] == 2
        indices = [s["index"] for s in data["steps"]]
        assert 1 not in indices
        assert 0 in indices
        assert 2 in indices

    def test_dropped_png_deleted_from_disk(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        # drop step 0 only
        responses = iter(["d", "k", "k"])

        run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert not (session / "screenshots" / "step_000.png").exists()
        assert (session / "screenshots" / "step_001.png").exists()
        assert (session / "screenshots" / "step_002.png").exists()

    def test_kept_pngs_not_deleted(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=3)
        responses = iter(["k", "k", "k"])

        run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        for i in range(3):
            assert (session / "screenshots" / f"step_{i:03d}.png").exists()

    def test_data_json_step_count_updated(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=4)
        # drop two
        responses = iter(["d", "k", "d", "k"])

        run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=lambda _: None,
            regenerate=False,
        )

        data = json.loads((session / "data.json").read_text(encoding="utf-8"))
        assert data["step_count"] == 2
        assert len(data["steps"]) == 2

    def test_drop_step_with_no_screenshot_does_not_raise(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=1)
        # Clear the screenshot field.
        data = json.loads((session / "data.json").read_text(encoding="utf-8"))
        data["steps"][0]["screenshot"] = ""
        (session / "data.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

        run_review_session(
            session,
            prompt_fn=lambda _: "d",
            echo_fn=lambda _: None,
            regenerate=False,
        )


# ---------------------------------------------------------------------------
# run_review_session - error handling
# ---------------------------------------------------------------------------

class TestReviewErrorHandling:

    def test_raises_when_data_json_missing(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="data.json not found"):
            run_review_session(
                empty_dir,
                prompt_fn=lambda _: "k",
                echo_fn=lambda _: None,
            )

    def test_empty_steps_returns_zero_counts(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=0)

        kept, dropped = run_review_session(
            session,
            prompt_fn=lambda _: "k",
            echo_fn=lambda _: None,
            regenerate=False,
        )

        assert kept == 0
        assert dropped == 0


# ---------------------------------------------------------------------------
# run_review_session - output messages
# ---------------------------------------------------------------------------

class TestReviewOutputMessages:

    def test_prints_step_action(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=1)
        output: list[str] = []

        run_review_session(
            session,
            prompt_fn=lambda _: "k",
            echo_fn=output.append,
            regenerate=False,
        )

        combined = " ".join(output)
        assert "Action 0" in combined

    def test_prints_program_name(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=1)
        output: list[str] = []

        run_review_session(
            session,
            prompt_fn=lambda _: "k",
            echo_fn=output.append,
            regenerate=False,
        )

        assert any("Chrome" in line for line in output)

    def test_prints_summary_at_end(self, tmp_path):
        session = _make_session_dir(tmp_path, step_count=2)
        output: list[str] = []
        responses = iter(["k", "d"])

        run_review_session(
            session,
            prompt_fn=lambda _: next(responses),
            echo_fn=output.append,
            regenerate=False,
        )

        summary = " ".join(output)
        assert "1 kept" in summary
        assert "1 dropped" in summary
