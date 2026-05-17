"""CLI integration tests for `sa replay` and `sa watch` commands."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from psr_ai.cli import main
from psr_ai.core.replay import ReplayConfig, ReplayResult
from psr_ai.core.watcher import WatchConfig, WatchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_zip(tmp_path: Path, title: str = "Test") -> Path:
    steps = [{"index": 0, "label": "Step 0", "action_text": "Click OK", "program": "App", "timestamp": ""}]
    data = {"title": title, "source": "x.zip", "generated_at": "", "step_count": 1, "steps": steps}
    p = tmp_path / "session.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("data.json", json.dumps(data))
        zf.writestr("screenshots/step_000.png", _TINY_PNG)
    return p


def _make_replay_result(success: bool = True) -> ReplayResult:
    r = ReplayResult(zip_path=Path("x.zip"), success=success, session_title="T", total_session_steps=1)
    return r


def _make_watch_result(success: bool = True) -> WatchResult:
    cfg = WatchConfig(trigger="x", action="y")
    r = WatchResult(config=cfg, success=success, stopped_reason="max_triggers" if success else "timeout")
    return r


# ---------------------------------------------------------------------------
# sa replay --help
# ---------------------------------------------------------------------------

class TestReplayHelp:

    def test_help_exits_0(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert result.exit_code == 0

    def test_help_shows_zip_argument(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "ZIP_PATH" in result.output

    def test_help_shows_dry_run(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "--dry-run" in result.output or "dry-run" in result.output

    def test_help_shows_no_dry_run(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "--no-dry-run" in result.output

    def test_help_shows_max_steps(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "--max-steps" in result.output

    def test_help_shows_provider(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "--provider" in result.output

    def test_help_shows_speed(self):
        result = CliRunner().invoke(main, ["replay", "--help"])
        assert "--speed" in result.output


# ---------------------------------------------------------------------------
# sa replay - exit codes
# ---------------------------------------------------------------------------

class TestReplayExitCodes:

    def test_success_exits_0(self, tmp_path):
        zip_path = _make_zip(tmp_path)
        with patch("psr_ai.core.replay.run_replay", return_value=_make_replay_result(True)):
            result = CliRunner().invoke(main, ["replay", str(zip_path)])
        assert result.exit_code == 0

    def test_failure_exits_1(self, tmp_path):
        zip_path = _make_zip(tmp_path)
        with patch("psr_ai.core.replay.run_replay", return_value=_make_replay_result(False)):
            result = CliRunner().invoke(main, ["replay", str(zip_path)])
        assert result.exit_code == 1

    def test_nonexistent_zip_fails(self, tmp_path):
        result = CliRunner().invoke(main, ["replay", str(tmp_path / "missing.zip")])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# sa replay - config propagation
# ---------------------------------------------------------------------------

class TestReplayConfigPropagation:

    def _invoke(self, tmp_path, *args):
        zip_path = _make_zip(tmp_path)
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_replay_result(True)

        with patch("psr_ai.core.replay.run_replay", side_effect=fake_run):
            CliRunner().invoke(main, ["replay", str(zip_path)] + list(args))

        return received[0] if received else None

    def test_dry_run_true_by_default(self, tmp_path):
        cfg = self._invoke(tmp_path)
        assert cfg.dry_run is True

    def test_no_dry_run_sets_false(self, tmp_path):
        cfg = self._invoke(tmp_path, "--no-dry-run")
        assert cfg.dry_run is False

    def test_max_steps_propagated(self, tmp_path):
        cfg = self._invoke(tmp_path, "--max-steps", "42")
        assert cfg.max_steps == 42

    def test_provider_propagated(self, tmp_path):
        cfg = self._invoke(tmp_path, "--provider", "openai")
        assert cfg.provider == "openai"

    def test_speed_propagated(self, tmp_path):
        cfg = self._invoke(tmp_path, "--speed", "2.0")
        assert cfg.speed == pytest.approx(2.0)

    def test_autonomous_warning_printed(self, tmp_path):
        zip_path = _make_zip(tmp_path)
        with patch("psr_ai.core.replay.run_replay", return_value=_make_replay_result(True)):
            result = CliRunner().invoke(
                main, ["replay", str(zip_path), "--no-dry-run", "--no-ask"]
            )
        assert "autonomous" in result.output.lower() or "WARNING" in result.output


# ---------------------------------------------------------------------------
# sa watch --help
# ---------------------------------------------------------------------------

class TestWatchHelp:

    def test_help_exits_0(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert result.exit_code == 0

    def test_help_shows_trigger_argument(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "TRIGGER" in result.output

    def test_help_shows_action_option(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "--action" in result.output

    def test_help_shows_interval(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "--interval" in result.output

    def test_help_shows_timeout(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "--timeout" in result.output

    def test_help_shows_max_triggers(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "--max-triggers" in result.output

    def test_help_shows_dry_run(self):
        result = CliRunner().invoke(main, ["watch", "--help"])
        assert "--dry-run" in result.output or "dry-run" in result.output


# ---------------------------------------------------------------------------
# sa watch - exit codes
# ---------------------------------------------------------------------------

class TestWatchExitCodes:

    def test_success_exits_0(self):
        with patch("psr_ai.core.watcher.run_watcher", return_value=_make_watch_result(True)):
            result = CliRunner().invoke(
                main, ["watch", "error dialog", "--action", "close it"]
            )
        assert result.exit_code == 0

    def test_failure_exits_1(self):
        with patch("psr_ai.core.watcher.run_watcher", return_value=_make_watch_result(False)):
            result = CliRunner().invoke(
                main, ["watch", "error dialog", "--action", "close it"]
            )
        assert result.exit_code == 1

    def test_missing_action_option_fails(self):
        result = CliRunner().invoke(main, ["watch", "some trigger"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# sa watch - config propagation
# ---------------------------------------------------------------------------

class TestWatchConfigPropagation:

    def _invoke(self, *args):
        received = []

        def fake_run(config, **kwargs):
            received.append(config)
            return _make_watch_result(True)

        with patch("psr_ai.core.watcher.run_watcher", side_effect=fake_run):
            CliRunner().invoke(
                main,
                ["watch", "trigger text", "--action", "do something"] + list(args),
            )

        return received[0] if received else None

    def test_trigger_propagated(self):
        cfg = self._invoke()
        assert cfg.trigger == "trigger text"

    def test_action_propagated(self):
        cfg = self._invoke()
        assert cfg.action == "do something"

    def test_dry_run_true_by_default(self):
        cfg = self._invoke()
        assert cfg.dry_run is True

    def test_no_dry_run_sets_false(self):
        cfg = self._invoke("--no-dry-run")
        assert cfg.dry_run is False

    def test_interval_propagated(self):
        cfg = self._invoke("--interval", "10.0")
        assert cfg.interval == pytest.approx(10.0)

    def test_timeout_propagated(self):
        cfg = self._invoke("--timeout", "120.0")
        assert cfg.timeout == pytest.approx(120.0)

    def test_max_triggers_propagated(self):
        cfg = self._invoke("--max-triggers", "3")
        assert cfg.max_triggers == 3

    def test_provider_propagated(self):
        cfg = self._invoke("--provider", "openai")
        assert cfg.provider == "openai"

    def test_autonomous_warning_printed(self):
        with patch("psr_ai.core.watcher.run_watcher", return_value=_make_watch_result(True)):
            result = CliRunner().invoke(
                main,
                ["watch", "trigger", "--action", "act", "--no-dry-run", "--no-ask"],
            )
        assert "autonomous" in result.output.lower() or "WARNING" in result.output
