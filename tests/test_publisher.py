"""Unit tests for core/publisher.py - Publisher class and helper functions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from psr_ai.core.publisher import (
    Publisher,
    PublishError,
    _ASSET_DIRS,
    _ASSET_FILES,
    publish,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_source_dir(tmp_path: Path, include_gif: bool = True) -> Path:
    """Create a realistic session output directory with all expected assets."""
    src = tmp_path / "psr_20240115_143000_output"
    src.mkdir()

    (src / "report.md").write_text("# Test Report\n", encoding="utf-8")
    (src / "data.json").write_text('{"title": "Test", "steps": []}', encoding="utf-8")
    if include_gif:
        (src / "action_replay.gif").write_bytes(b"GIF89a")

    screenshots = src / "screenshots"
    screenshots.mkdir()
    (screenshots / "step_000.png").write_bytes(b"\x89PNG\r\n")
    (screenshots / "step_001.png").write_bytes(b"\x89PNG\r\n")

    return src


# ---------------------------------------------------------------------------
# Publisher.copy
# ---------------------------------------------------------------------------

class TestPublisherCopy:

    def test_creates_dest_dir(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "docs" / "sessions"
        pub = Publisher(source_dir=src, dest_root=dest_root)

        pub.copy()

        assert (dest_root / src.name).is_dir()

    def test_copies_report_md(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()

        assert (dest / "report.md").exists()
        assert (dest / "report.md").read_text(encoding="utf-8") == "# Test Report\n"

    def test_copies_data_json(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()

        data = json.loads((dest / "data.json").read_text(encoding="utf-8"))
        assert data["title"] == "Test"

    def test_copies_gif(self, tmp_path):
        src = _make_source_dir(tmp_path, include_gif=True)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()

        assert (dest / "action_replay.gif").exists()
        assert (dest / "action_replay.gif").read_bytes() == b"GIF89a"

    def test_copies_screenshots_dir(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()

        assert (dest / "screenshots").is_dir()
        assert (dest / "screenshots" / "step_000.png").exists()
        assert (dest / "screenshots" / "step_001.png").exists()

    def test_skips_missing_gif(self, tmp_path):
        # GIF is optional - session may have been run with --no-gif.
        src = _make_source_dir(tmp_path, include_gif=False)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()

        assert not (dest / "action_replay.gif").exists()
        # Other assets must still be copied.
        assert (dest / "report.md").exists()

    def test_overwrites_existing_screenshots_dir(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, dest_root=tmp_path / "out")

        dest = pub.copy()
        # Write a stale file into screenshots.
        (dest / "screenshots" / "stale.png").write_bytes(b"old")

        # Second copy should replace the directory cleanly.
        pub.copy()
        assert not (dest / "screenshots" / "stale.png").exists()

    def test_returns_destination_path(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "pub"
        pub = Publisher(source_dir=src, dest_root=dest_root)

        result = pub.copy()

        assert result == dest_root / src.name

    def test_default_dest_root_is_docs_sessions(self, tmp_path):
        src = _make_source_dir(tmp_path)
        # No dest_root provided: should use cwd / docs / sessions.
        with patch("psr_ai.core.publisher.Path.cwd", return_value=tmp_path):
            pub = Publisher(source_dir=src)

        assert pub.dest_root == tmp_path / "docs" / "sessions"

    def test_asset_files_constant(self):
        # Smoke-check the module constant so test expectations stay in sync.
        assert "report.md" in _ASSET_FILES
        assert "data.json" in _ASSET_FILES
        assert "action_replay.gif" in _ASSET_FILES

    def test_asset_dirs_constant(self):
        assert "screenshots" in _ASSET_DIRS


# ---------------------------------------------------------------------------
# Publisher._git (internal runner)
# ---------------------------------------------------------------------------

class TestPublisherGitRunner:

    def test_raises_when_git_not_found(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        with patch("psr_ai.core.publisher.subprocess.run",
                   side_effect=FileNotFoundError):
            with pytest.raises(PublishError, match="git executable not found"):
                pub._git("status")

    def test_raises_on_nonzero_exit(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: not a git repository"
        with patch("psr_ai.core.publisher.subprocess.run", return_value=mock_result):
            with pytest.raises(PublishError, match="fatal: not a git repository"):
                pub._git("status")

    def test_returns_completed_process_on_success(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M report.md\n"
        with patch("psr_ai.core.publisher.subprocess.run", return_value=mock_result):
            result = pub._git("status", "--short")

        assert result.stdout == "M report.md\n"

    def test_uses_repo_root_as_cwd(self, tmp_path):
        src = _make_source_dir(tmp_path)
        repo = tmp_path / "myrepo"
        repo.mkdir()
        pub = Publisher(source_dir=src, repo_root=repo)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("psr_ai.core.publisher.subprocess.run",
                   return_value=mock_result) as mock_run:
            pub._git("status")

        called_cwd = mock_run.call_args.kwargs.get("cwd") or mock_run.call_args[1].get("cwd")
        assert called_cwd == str(repo)


# ---------------------------------------------------------------------------
# Publisher.is_git_repo
# ---------------------------------------------------------------------------

class TestIsGitRepo:

    def test_true_when_git_succeeds(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ".git"
        with patch("psr_ai.core.publisher.subprocess.run", return_value=mock_result):
            assert pub.is_git_repo() is True

    def test_false_when_git_fails(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "fatal: not a git repository"
        with patch("psr_ai.core.publisher.subprocess.run", return_value=mock_result):
            assert pub.is_git_repo() is False


# ---------------------------------------------------------------------------
# Publisher.git_status
# ---------------------------------------------------------------------------

class TestGitStatus:

    def test_returns_stdout(self, tmp_path):
        src = _make_source_dir(tmp_path)
        pub = Publisher(source_dir=src, repo_root=tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "M docs/sessions/report.md\n"
        with patch("psr_ai.core.publisher.subprocess.run", return_value=mock_result):
            result = pub.git_status()

        assert "report.md" in result


# ---------------------------------------------------------------------------
# Publisher.commit_and_push
# ---------------------------------------------------------------------------

class TestCommitAndPush:

    def _mock_git(self, responses: list[MagicMock]):
        """Return a side_effect list for subprocess.run mapping to git calls."""
        return responses

    def _ok(self, stdout: str = "") -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_calls_git_add_commit_push_revparse(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "docs" / "sessions"
        pub = Publisher(source_dir=src, dest_root=dest_root, repo_root=tmp_path)
        dest = dest_root / src.name
        dest.mkdir(parents=True, exist_ok=True)

        sha = "abc1234567890"
        with patch("psr_ai.core.publisher.subprocess.run", side_effect=[
            self._ok(),               # git add
            self._ok(),               # git commit
            self._ok(),               # git push
            self._ok(sha + "\n"),     # git rev-parse HEAD
        ]) as mock_run:
            result = pub.commit_and_push(dest, message="test commit")

        assert result == sha
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0][1] == "add"
        assert calls[1][1] == "commit"
        assert calls[2][1] == "push"
        assert calls[3][1] == "rev-parse"

    def test_default_message_contains_session_name(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "out"
        pub = Publisher(source_dir=src, dest_root=dest_root, repo_root=tmp_path)
        dest = dest_root / src.name
        dest.mkdir(parents=True, exist_ok=True)

        captured_messages = []

        def capture_run(cmd, **kwargs):
            if "commit" in cmd:
                # The message is always at cmd[-1] when passed with -m.
                captured_messages.append(cmd[-1])
            m = MagicMock()
            m.returncode = 0
            m.stdout = "deadbeef\n"
            m.stderr = ""
            return m

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=capture_run):
            pub.commit_and_push(dest)

        assert captured_messages
        assert src.name in captured_messages[0]

    def test_custom_message_used(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "out"
        pub = Publisher(source_dir=src, dest_root=dest_root, repo_root=tmp_path)
        dest = dest_root / src.name
        dest.mkdir(parents=True, exist_ok=True)

        captured_messages = []

        def capture_run(cmd, **kwargs):
            if "commit" in cmd:
                captured_messages.append(cmd[-1])
            m = MagicMock()
            m.returncode = 0
            m.stdout = "abc\n"
            m.stderr = ""
            return m

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=capture_run):
            pub.commit_and_push(dest, message="custom: my message")

        assert captured_messages[0] == "custom: my message"

    def test_raises_publish_error_on_git_failure(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "out"
        pub = Publisher(source_dir=src, dest_root=dest_root, repo_root=tmp_path)
        dest = dest_root / src.name
        dest.mkdir(parents=True, exist_ok=True)

        fail = MagicMock()
        fail.returncode = 1
        fail.stderr = "error: failed to push"
        fail.stdout = ""

        ok = MagicMock()
        ok.returncode = 0
        ok.stdout = ""
        ok.stderr = ""

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=[ok, ok, fail]):
            with pytest.raises(PublishError, match="failed to push"):
                pub.commit_and_push(dest)

    def test_push_includes_branch_when_specified(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "out"
        pub = Publisher(source_dir=src, dest_root=dest_root, repo_root=tmp_path)
        dest = dest_root / src.name
        dest.mkdir(parents=True, exist_ok=True)

        captured_push_cmd = []

        def capture_run(cmd, **kwargs):
            if "push" in cmd:
                captured_push_cmd.extend(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "sha\n"
            m.stderr = ""
            return m

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=capture_run):
            pub.commit_and_push(dest, remote="upstream", branch="docs-branch")

        assert "upstream" in captured_push_cmd
        assert "docs-branch" in captured_push_cmd


# ---------------------------------------------------------------------------
# publish() backward-compatible wrapper
# ---------------------------------------------------------------------------

class TestPublishWrapper:

    def test_local_calls_publisher_copy(self, tmp_path):
        src = _make_source_dir(tmp_path)
        dest_root = tmp_path / "docs" / "sessions"

        with patch("psr_ai.core.publisher.Path.cwd", return_value=tmp_path):
            result = publish(src, target="local")

        assert Path(result).exists()
        assert (Path(result) / "report.md").exists()

    def test_github_calls_api_function(self, tmp_path):
        src = _make_source_dir(tmp_path)
        with patch("psr_ai.core.publisher.publish_via_github_api",
                   return_value="https://github.com/example") as mock_api:
            result = publish(src, target="github")

        mock_api.assert_called_once_with(src)
        assert result == "https://github.com/example"
