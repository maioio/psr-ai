"""Tests for psr_ai.core.downloader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# _ytdlp_available / _gallery_dl_available
# ---------------------------------------------------------------------------

class TestAvailabilityChecks:
    def test_ytdlp_available_returns_true_on_success(self):
        from psr_ai.core.downloader import _ytdlp_available

        with patch("subprocess.run", return_value=_make_completed_process(0)):
            assert _ytdlp_available() is True

    def test_ytdlp_available_returns_false_on_not_found(self):
        from psr_ai.core.downloader import _ytdlp_available

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _ytdlp_available() is False

    def test_gallery_dl_available_false_on_not_found(self):
        from psr_ai.core.downloader import _gallery_dl_available

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _gallery_dl_available() is False


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------

class TestDownloadMedia:
    def test_raises_import_error_when_ytdlp_missing(self, tmp_path):
        from psr_ai.core.downloader import download_media

        with patch("psr_ai.core.downloader._ytdlp_available", return_value=False):
            with pytest.raises(ImportError, match="yt-dlp"):
                download_media("https://example.com/video", tmp_path)

    def test_successful_download_returns_files(self, tmp_path):
        from psr_ai.core.downloader import download_media

        def _fake_run(cmd, **kw):
            # Simulate yt-dlp creating an output file
            (tmp_path / "video.mp4").write_bytes(b"fake video")
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._ytdlp_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            result = download_media("https://youtube.com/watch?v=xxx", tmp_path)

        assert result.tool_used == "yt-dlp"
        assert result.success

    def test_failed_ytdlp_no_gallery_returns_error(self, tmp_path):
        from psr_ai.core.downloader import download_media

        with patch("psr_ai.core.downloader._ytdlp_available", return_value=True), \
             patch("psr_ai.core.downloader._gallery_dl_available", return_value=False), \
             patch("subprocess.run", return_value=_make_completed_process(1, stderr="not found")):
            result = download_media("https://example.com/broken", tmp_path)

        assert result.error is not None
        assert not result.success

    def test_report_shows_tool_used(self, tmp_path):
        from psr_ai.core.downloader import download_media

        def _fake_run(cmd, **kw):
            (tmp_path / "out.mp4").write_bytes(b"fake")
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._ytdlp_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            result = download_media("https://youtube.com/watch?v=abc", tmp_path)

        assert "yt-dlp" in result.report()

    def test_audio_only_flag_included_in_cmd(self, tmp_path):
        from psr_ai.core.downloader import download_media

        dummy = tmp_path / "out.mp3"
        dummy.write_bytes(b"fake audio")

        captured_cmd = []

        def _fake_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._ytdlp_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            download_media("https://youtube.com/watch?v=abc", tmp_path, audio_only=True)

        assert "-x" in captured_cmd


# ---------------------------------------------------------------------------
# download_profile
# ---------------------------------------------------------------------------

class TestDownloadProfile:
    def test_raises_if_neither_tool_available(self, tmp_path):
        from psr_ai.core.downloader import download_profile

        with patch("psr_ai.core.downloader._gallery_dl_available", return_value=False), \
             patch("psr_ai.core.downloader._ytdlp_available", return_value=False):
            with pytest.raises(ImportError, match="gallery-dl or yt-dlp"):
                download_profile("https://instagram.com/user/", tmp_path)

    def test_uses_gallery_dl_when_available(self, tmp_path):
        from psr_ai.core.downloader import download_profile

        dummy = tmp_path / "photo.jpg"
        dummy.write_bytes(b"jpg")

        captured_cmd = []

        def _fake_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._gallery_dl_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            result = download_profile("https://instagram.com/user/", tmp_path)

        assert result.tool_used == "gallery-dl"
        assert "gallery-dl" in captured_cmd

    def test_falls_back_to_ytdlp(self, tmp_path):
        from psr_ai.core.downloader import download_profile

        dummy = tmp_path / "vid.mp4"
        dummy.write_bytes(b"vid")

        captured_cmd = []

        def _fake_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._gallery_dl_available", return_value=False), \
             patch("psr_ai.core.downloader._ytdlp_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            result = download_profile("https://youtube.com/channel/xxx", tmp_path)

        assert result.tool_used == "yt-dlp"

    def test_limit_passed_to_gallery_dl(self, tmp_path):
        from psr_ai.core.downloader import download_profile

        captured_cmd = []

        def _fake_run(cmd, **kw):
            captured_cmd.extend(cmd)
            (tmp_path / "x.jpg").write_bytes(b"x")
            return _make_completed_process(0)

        with patch("psr_ai.core.downloader._gallery_dl_available", return_value=True), \
             patch("subprocess.run", side_effect=_fake_run):
            download_profile("https://example.com/user", tmp_path, limit=20)

        assert "1-20" in " ".join(captured_cmd)


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestDownloaderCLI:
    def test_download_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["download", "--help"])
        assert result.exit_code == 0

    def test_download_profile_command_exists(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["download-profile", "--help"])
        assert result.exit_code == 0

    def test_download_help_mentions_audio(self):
        from click.testing import CliRunner
        from psr_ai.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["download", "--help"])
        assert "audio" in result.output.lower()
