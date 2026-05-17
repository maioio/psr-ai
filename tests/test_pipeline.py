"""Unit tests for core/pipeline.py and the Publisher branch-creation additions."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from psr_ai.core.pipeline import PipelineConfig, PipelineResult, run_pipeline
from psr_ai.core.publisher import Publisher, PublishError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_zip(tmp_path: Path, num_images: int = 1) -> Path:
    """Create a minimal PSR-style ZIP for pipeline tests."""
    boundary = "----=_TEST"
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
    step_html = "".join(
        f'<p class="step">Step {i}: <b>Click {i}</b></p>' for i in range(num_images)
    )
    html_body = f"<html><head><title>Pipeline Test</title></head><body>{step_html}</body></html>"
    html_part = (
        f"--{boundary}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: quoted-printable\r\n\r\n{html_body}\r\n"
    )
    img_parts = "".join(
        f"--{boundary}\r\nContent-Type: image/png\r\n"
        f"Content-Transfer-Encoding: base64\r\n"
        f"Content-Location: step_{i:03d}.png\r\n\r\n{b64_lines}\r\n"
        for i in range(num_images)
    )
    mht = (
        f"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"{boundary}\"\r\n\r\n"
        f"{html_part}{img_parts}--{boundary}--\r\n"
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("recording.mht", mht)
    zip_path = tmp_path / "recording.zip"
    zip_path.write_bytes(buf.getvalue())
    return zip_path


def _make_config(tmp_path: Path, **overrides) -> PipelineConfig:
    zip_path = _make_zip(tmp_path)
    defaults = dict(
        zip_path=zip_path,
        project_root=tmp_path,
        branch=None,
        create_branch=False,
        analyze=False,
        github_pr=False,
        no_gif=True,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


# ---------------------------------------------------------------------------
# run_pipeline: basic flow
# ---------------------------------------------------------------------------

class TestRunPipeline:

    def test_returns_pipeline_result(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert isinstance(result, PipelineResult)

    def test_success_when_zip_valid(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert result.success is True

    def test_output_dir_set_in_result(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert result.output_dir is not None
        assert result.output_dir.is_dir()

    def test_report_path_set(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert result.report_path is not None
        assert result.report_path.exists()

    def test_failure_on_missing_zip(self, tmp_path):
        config = PipelineConfig(
            zip_path=tmp_path / "nonexistent.zip",
            project_root=tmp_path,
            analyze=False,
            github_pr=False,
            no_gif=True,
        )
        result = run_pipeline(config)
        assert result.success is False
        assert result.error

    def test_echo_fn_called(self, tmp_path):
        config = _make_config(tmp_path)
        messages: list[str] = []
        run_pipeline(config, echo_fn=messages.append)
        assert any("Parsing" in m for m in messages)

    def test_steps_log_populated(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert any("parse" in s for s in result.steps)

    def test_no_gif_skips_gif(self, tmp_path):
        config = _make_config(tmp_path, no_gif=True)
        result = run_pipeline(config)
        assert result.gif_path is None

    def test_result_never_raises(self, tmp_path):
        """run_pipeline must catch all exceptions and set success=False."""
        config = _make_config(tmp_path)
        with patch("psr_ai.core.pipeline._parse_step", side_effect=RuntimeError("kaboom")):
            result = run_pipeline(config)
        assert result.success is False
        assert "kaboom" in result.error

    def test_analyze_step_skipped_when_disabled(self, tmp_path):
        config = _make_config(tmp_path, analyze=False)
        with patch("psr_ai.core.pipeline._analyze_step") as mock_analyze:
            run_pipeline(config)
        mock_analyze.assert_not_called()

    def test_analyze_step_called_when_enabled(self, tmp_path):
        config = _make_config(tmp_path, analyze=True)
        with patch("psr_ai.core.pipeline._analyze_step", return_value=1) as mock_analyze:
            result = run_pipeline(config)
        mock_analyze.assert_called_once()
        assert result.enriched_steps == 1

    def test_pr_skipped_when_disabled(self, tmp_path):
        config = _make_config(tmp_path, github_pr=False)
        with patch("psr_ai.core.pipeline._open_pr") as mock_pr:
            run_pipeline(config)
        mock_pr.assert_not_called()

    def test_pr_skipped_when_no_branch(self, tmp_path):
        config = _make_config(tmp_path, github_pr=True, branch=None)
        with patch("psr_ai.core.pipeline._open_pr") as mock_pr:
            run_pipeline(config)
        mock_pr.assert_not_called()

    def test_pr_called_when_enabled_with_branch(self, tmp_path):
        config = _make_config(tmp_path, github_pr=True, branch="docs/test")
        with patch("psr_ai.core.pipeline._open_pr", return_value="https://github.com/pr/1") as mock_pr:
            result = run_pipeline(config)
        mock_pr.assert_called_once()
        assert result.pr_url == "https://github.com/pr/1"


# ---------------------------------------------------------------------------
# PipelineResult.report
# ---------------------------------------------------------------------------

class TestPipelineResultReport:

    def test_report_contains_status(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert "SUCCESS" in result.report() or "FAILED" in result.report()

    def test_report_contains_zip_name(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        report = result.report()
        assert "recording.zip" in report

    def test_report_contains_error_when_failed(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("psr_ai.core.pipeline._parse_step", side_effect=RuntimeError("disk full")):
            result = run_pipeline(config)
        assert "disk full" in result.report()

    def test_report_contains_no_em_dashes(self, tmp_path):
        config = _make_config(tmp_path)
        result = run_pipeline(config)
        assert "—" not in result.report()


# ---------------------------------------------------------------------------
# Publisher: branch creation additions
# ---------------------------------------------------------------------------

class TestPublisherBranchCreation:

    def _make_pub(self, tmp_path: Path) -> Publisher:
        return Publisher(source_dir=tmp_path, repo_root=tmp_path)

    def test_create_branch_calls_git_checkout_b(self, tmp_path):
        pub = self._make_pub(tmp_path)
        with patch.object(pub, "_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="", returncode=0)
            pub.create_branch("docs/new-feature")
        mock_git.assert_called_with("checkout", "-b", "docs/new-feature")

    def test_current_branch_returns_branch_name(self, tmp_path):
        pub = self._make_pub(tmp_path)
        mock_result = MagicMock()
        mock_result.stdout = "main\n"
        with patch.object(pub, "_git", return_value=mock_result):
            assert pub.current_branch() == "main"

    def test_branch_exists_remote_true_when_output_nonempty(self, tmp_path):
        pub = self._make_pub(tmp_path)
        mock_result = MagicMock()
        mock_result.stdout = "abc123  refs/heads/my-branch\n"
        with patch.object(pub, "_git", return_value=mock_result):
            assert pub.branch_exists_remote("my-branch") is True

    def test_branch_exists_remote_false_when_output_empty(self, tmp_path):
        pub = self._make_pub(tmp_path)
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch.object(pub, "_git", return_value=mock_result):
            assert pub.branch_exists_remote("nonexistent") is False

    def test_branch_exists_remote_false_on_publish_error(self, tmp_path):
        pub = self._make_pub(tmp_path)
        with patch.object(pub, "_git", side_effect=PublishError("no remote")):
            assert pub.branch_exists_remote("any") is False

    def test_commit_and_push_creates_branch_when_flag_set(self, tmp_path):
        pub = self._make_pub(tmp_path)
        dest = tmp_path / "session"
        dest.mkdir()

        git_calls: list[tuple] = []
        sha_result = MagicMock(stdout="abc123sha\n")

        def fake_git(*args):
            git_calls.append(args)
            return sha_result

        with patch.object(pub, "_git", side_effect=fake_git):
            pub.commit_and_push(dest, branch="docs/auto", create_branch=True)

        # First call must be checkout -b
        assert git_calls[0] == ("checkout", "-b", "docs/auto")

    def test_commit_and_push_no_branch_creation_when_flag_false(self, tmp_path):
        pub = self._make_pub(tmp_path)
        dest = tmp_path / "session"
        dest.mkdir()
        git_calls: list[tuple] = []
        sha_result = MagicMock(stdout="abc123\n")

        with patch.object(pub, "_git", side_effect=lambda *a: (git_calls.append(a) or sha_result)):
            pub.commit_and_push(dest, branch="existing-branch", create_branch=False)

        assert ("checkout", "-b", "existing-branch") not in git_calls

    def test_commit_and_push_uses_set_upstream_when_creating(self, tmp_path):
        pub = self._make_pub(tmp_path)
        dest = tmp_path / "session"
        dest.mkdir()
        git_calls: list[tuple] = []
        sha_result = MagicMock(stdout="sha\n")

        with patch.object(pub, "_git", side_effect=lambda *a: (git_calls.append(a) or sha_result)):
            pub.commit_and_push(dest, branch="new-branch", create_branch=True)

        # Find the push call and verify -u (short for --set-upstream) is present.
        push_calls = [c for c in git_calls if c[0] == "push"]
        assert push_calls, "No push call was made"
        push_args = push_calls[0]
        assert "-u" in push_args
