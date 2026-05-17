"""Security and hardening tests.

Covers:
  - xprop window-ID validation (prevents external-process output injection)
  - subprocess argument construction (no shell=True leakage into non-Windows paths)
  - Corrupted / oversized ZIP handling in parse_session
  - Truncated MHT handling
  - publisher._git argument construction (no shell injection via commit message)
  - safe_path traversal guard
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.recorder import (
    LinuxRecorder,
    MacRecorder,
    WindowsRecorder,
    _RE_X11_WINDOW_ID,
)
from psr_ai.core.parser import parse_session
from psr_ai.core.publisher import Publisher, PublishError
from psr_ai.utils.paths import safe_path


# ---------------------------------------------------------------------------
# _RE_X11_WINDOW_ID: validates hex window IDs before subprocess injection
# ---------------------------------------------------------------------------

class TestX11WindowIdPattern:
    """The compiled regex must accept valid hex IDs and reject all others."""

    def test_accepts_standard_hex_id(self):
        assert _RE_X11_WINDOW_ID.match("0x3a00006") is not None

    def test_accepts_uppercase_hex(self):
        assert _RE_X11_WINDOW_ID.match("0xABCDEF") is not None

    def test_accepts_mixed_case_hex(self):
        assert _RE_X11_WINDOW_ID.match("0x1a2B3c") is not None

    def test_accepts_minimal_hex_id(self):
        assert _RE_X11_WINDOW_ID.match("0x1") is not None

    def test_rejects_decimal_integer(self):
        assert _RE_X11_WINDOW_ID.match("12345") is None

    def test_rejects_empty_string(self):
        assert _RE_X11_WINDOW_ID.match("") is None

    def test_rejects_shell_metacharacter_injection(self):
        # A crafted xprop output that could inject args if not validated.
        payloads = [
            "0x1; rm -rf /",
            "0x1 --option",
            "$(evil)",
            "`evil`",
            "0x1\x00null",
            "0xGGGG",       # invalid hex digits
            "0x",           # no digits after prefix
        ]
        for payload in payloads:
            assert _RE_X11_WINDOW_ID.match(payload) is None, (
                f"Pattern should reject: {payload!r}"
            )

    def test_rejects_whitespace_padded_id(self):
        # An ID with leading/trailing whitespace must not match (split() strips it,
        # but the validator must also reject it in case the caller changes).
        assert _RE_X11_WINDOW_ID.match(" 0x3a00006") is None
        assert _RE_X11_WINDOW_ID.match("0x3a00006 ") is None


# ---------------------------------------------------------------------------
# LinuxRecorder._active_window_name: validation applied before subprocess call
# ---------------------------------------------------------------------------

class TestLinuxActiveWindowSecurity:

    def _make_recorder(self) -> LinuxRecorder:
        with patch("psr_ai.core.recorder.shutil.which", return_value=None):
            return LinuxRecorder()

    def test_rejects_malformed_xprop_id_silently(self):
        """When xprop returns a non-hex window ID, the second xprop call must not execute."""
        r = self._make_recorder()
        xprop_call_count = [0]

        def fake_which(name: str) -> str | None:
            # Only xprop is available; gdbus is absent so the fallback cannot run.
            return "/usr/bin/xprop" if name == "xprop" else None

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "xprop" in cmd[0]:
                xprop_call_count[0] += 1
                if cmd == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
                    # Malicious / unexpected output instead of a hex window ID.
                    result.stdout = "_NET_ACTIVE_WINDOW(WINDOW): window id # BADVALUE"
                else:
                    # This branch must never execute for the malformed-ID case.
                    result.stdout = '_NET_WM_NAME(UTF8_STRING) = "My App"'
            else:
                result.stdout = ""
            return result

        with (
            patch("psr_ai.core.recorder.shutil.which", side_effect=fake_which),
            patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run),
        ):
            name = r._active_window_name()

        # Only the root-query xprop call must have happened.
        # The window-name xprop call must not execute because the ID was invalid.
        assert xprop_call_count[0] == 1
        assert name == ""

    def test_accepts_valid_xprop_id_and_returns_title(self):
        r = self._make_recorder()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
                result.stdout = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00006"
            else:
                result.stdout = '_NET_WM_NAME(UTF8_STRING) = "Firefox"'
            return result

        with patch("psr_ai.core.recorder.shutil.which", return_value="/usr/bin/xprop"):
            with patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run):
                name = r._active_window_name()

        assert name == "Firefox"

    def test_empty_xprop_output_handled_gracefully(self):
        r = self._make_recorder()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            return result

        with patch("psr_ai.core.recorder.shutil.which", return_value="/usr/bin/xprop"):
            with patch("psr_ai.core.recorder.subprocess.run", side_effect=fake_run):
                name = r._active_window_name()

        assert name == ""

    def test_xprop_file_not_found_handled_gracefully(self):
        r = self._make_recorder()
        with patch("psr_ai.core.recorder.shutil.which", return_value="/usr/bin/xprop"):
            with patch("psr_ai.core.recorder.subprocess.run", side_effect=FileNotFoundError):
                name = r._active_window_name()
        assert name == ""


# ---------------------------------------------------------------------------
# WindowsRecorder: no shell=True on non-.lnk paths
# ---------------------------------------------------------------------------

class TestWindowsSubprocessSafety:

    def _make_recorder(self, tmp_path: Path) -> WindowsRecorder:
        fake_psr = tmp_path / "psr.exe"
        fake_psr.touch()
        r = object.__new__(WindowsRecorder)
        r._psr_exe = fake_psr
        r._pending_output = None
        return r

    def test_shell_false_for_exe_path(self, tmp_path):
        r = self._make_recorder(tmp_path)
        assert r._use_shell is False

    def test_popen_called_with_shell_false(self, tmp_path):
        r = self._make_recorder(tmp_path)
        with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
            r.start_recording()
        _, kwargs = mock_popen.call_args
        assert kwargs.get("shell") is False

    def test_popen_args_are_list_not_string(self, tmp_path):
        r = self._make_recorder(tmp_path)
        with patch("psr_ai.core.recorder.subprocess.Popen") as mock_popen:
            r.start_recording()
        args_list = mock_popen.call_args[0][0]
        assert isinstance(args_list, list)

    def test_no_user_data_in_popen_args(self, tmp_path):
        r = self._make_recorder(tmp_path)
        captured_args = []
        with patch("psr_ai.core.recorder.subprocess.Popen",
                   side_effect=lambda a, **k: captured_args.extend(a) or MagicMock()):
            r.start_recording()
        # All elements must be either a str representation of a known-safe Path
        # or a literal constant. None should contain shell metacharacters.
        for arg in captured_args:
            assert "$" not in arg
            assert ";" not in arg
            assert "|" not in arg
            assert "&" not in arg


# ---------------------------------------------------------------------------
# publisher._git: list-form subprocess, no shell injection
# ---------------------------------------------------------------------------

class TestPublisherGitSafety:

    def test_git_called_as_list_not_string(self, tmp_path):
        pub = Publisher(source_dir=tmp_path, repo_root=tmp_path)
        captured = []
        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r
        with patch("psr_ai.core.recorder.subprocess.run"):
            with patch("psr_ai.core.publisher.subprocess.run", side_effect=fake_run):
                pub._git("rev-parse", "HEAD")
        assert isinstance(captured[0], list)
        assert captured[0][0] == "git"

    def test_commit_message_is_separate_list_element(self, tmp_path):
        """User-supplied commit message must be a discrete list element, not
        shell-interpolated. This ensures a message like '; rm -rf /' cannot
        execute as a shell command."""
        pub = Publisher(source_dir=tmp_path, repo_root=tmp_path)
        evil_message = "legit docs; rm -rf /"
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "sha\n"
            r.stderr = ""
            return r

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=fake_run):
            pub._git("commit", "-m", evil_message)

        # The message must appear verbatim as a single element, not shell-split.
        commit_cmd = captured_cmds[0]
        assert evil_message in commit_cmd
        # Specifically the -m flag and the message must be adjacent list elements.
        m_index = commit_cmd.index("-m")
        assert commit_cmd[m_index + 1] == evil_message

    def test_git_raises_publish_error_on_nonzero_exit(self, tmp_path):
        pub = Publisher(source_dir=tmp_path, repo_root=tmp_path)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 128
            r.stdout = ""
            r.stderr = "fatal: not a git repository"
            return r

        with patch("psr_ai.core.publisher.subprocess.run", side_effect=fake_run):
            with pytest.raises(PublishError, match="git rev-parse failed"):
                pub._git("rev-parse", "--git-dir")

    def test_git_raises_publish_error_when_git_not_on_path(self, tmp_path):
        pub = Publisher(source_dir=tmp_path, repo_root=tmp_path)
        with patch("psr_ai.core.publisher.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(PublishError, match="git executable not found"):
                pub._git("status")


# ---------------------------------------------------------------------------
# parse_session: corrupted and boundary-case ZIP inputs
# ---------------------------------------------------------------------------

class TestParserResiliency:

    def test_corrupted_zip_raises_value_error(self, tmp_path):
        bad = tmp_path / "corrupt.zip"
        bad.write_bytes(b"\x00" * 512)
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            parse_session(bad)

    def test_empty_file_raises_value_error(self, tmp_path):
        empty = tmp_path / "empty.zip"
        empty.write_bytes(b"")
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            parse_session(empty)

    def test_zip_without_mht_raises_value_error(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "no mht")
        zipped = tmp_path / "no_mht.zip"
        zipped.write_bytes(buf.getvalue())
        with pytest.raises(ValueError, match="No .mht file"):
            parse_session(zipped)

    def test_zip_with_empty_mht_returns_empty_session(self, tmp_path):
        """A zero-byte MHT must not crash the parser; it must return 0 steps."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("recording.mht", b"")
        zipped = tmp_path / "empty_mht.zip"
        zipped.write_bytes(buf.getvalue())
        session = parse_session(zipped)
        assert len(session.steps) == 0

    def test_zip_with_truncated_mht_returns_partial_session(self, tmp_path):
        """A truncated MHT (simulates mid-write shutdown) must not raise.
        The parser must return whatever steps it managed to extract."""
        partial_mht = b"MIME-Version: 1.0\r\nContent-Type: text/html\r\n\r\n<html><body>incomplete"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("recording.mht", partial_mht)
        zipped = tmp_path / "truncated_mht.zip"
        zipped.write_bytes(buf.getvalue())
        # Must not raise; returns a session with 0 or more steps.
        session = parse_session(zipped)
        assert isinstance(session.steps, list)

    def test_oversized_image_data_does_not_crash(self, tmp_path):
        """A step with a very large base64 payload must be decoded without raising."""
        import base64
        large_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)  # 2 MB
        b64 = base64.b64encode(large_bytes).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        boundary = "----=_BIG"
        mht = (
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/related; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: image/png\r\n"
            f"Content-Transfer-Encoding: base64\r\n"
            f"Content-Location: step_000.png\r\n"
            f"\r\n"
            f"{b64_lines}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("recording.mht", mht)
        zipped = tmp_path / "big.zip"
        zipped.write_bytes(buf.getvalue())
        session = parse_session(zipped)
        assert len(session.steps) == 1


# ---------------------------------------------------------------------------
# safe_path traversal guard
# ---------------------------------------------------------------------------

class TestSafePathTraversalGuard:

    def test_rejects_path_traversal_outside_base(self, tmp_path):
        base = tmp_path / "allowed"
        base.mkdir()
        with pytest.raises(ValueError, match="outside the allowed base"):
            safe_path("../../etc/passwd", base=base)

    def test_accepts_path_inside_base(self, tmp_path):
        base = tmp_path / "allowed"
        base.mkdir()
        target = base / "session" / "data.json"
        result = safe_path(target, base=base)
        assert str(result).startswith(str(base.resolve()))

    def test_rejects_absolute_path_outside_base(self, tmp_path):
        base = tmp_path / "allowed"
        base.mkdir()
        with pytest.raises(ValueError, match="outside the allowed base"):
            safe_path("/etc/passwd", base=base)
