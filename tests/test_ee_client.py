"""Tests for psr_ai.ee.client — PSRCloudClient async upload with error handling.

Coverage:
  - _sanitize_headers: redacts sensitive keys, passes safe keys through
  - upload(): missing data.json raises FileNotFoundError
  - upload(): successful POST returns session_id and status
  - upload(): 429 triggers backoff and retry, then succeeds
  - upload(): 500 triggers backoff and retry, then succeeds
  - upload(): persistent failure after MAX_RETRIES raises RuntimeError
  - upload(): context manager closes session on exit
  - upload(): no auth token appears in any log output
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from psr_ai.ee.client import (
    PSRCloudClient,
    _SENSITIVE_HEADERS,
    _sanitize_headers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_dir(tmp_path: Path, step_count: int = 3, source: str = "test-session") -> Path:
    """Write a minimal data.json and return the session directory."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "data.json").write_text(
        json.dumps({"step_count": step_count, "source": source}),
        encoding="utf-8",
    )
    return session_dir


def _mock_response(status: int, body: dict | None = None, headers: dict | None = None):
    """Build an AsyncMock that behaves like an aiohttp response context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=body or {})
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")

    # async context manager protocol
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, resp


def _make_client(endpoint: str = "https://cloud.psr.ai", token: str = "tok-test") -> PSRCloudClient:
    return PSRCloudClient(endpoint=endpoint, token=token, team="test-team")


# ---------------------------------------------------------------------------
# _sanitize_headers
# ---------------------------------------------------------------------------

class TestSanitizeHeaders:

    def test_redacts_all_sensitive_keys(self):
        headers = {k: "real-value" for k in _SENSITIVE_HEADERS}
        result = _sanitize_headers(headers)
        for k in _SENSITIVE_HEADERS:
            assert result[k] == "***REDACTED***", f"Expected redaction for {k}"

    def test_passes_safe_headers_through(self):
        headers = {
            "content-type": "application/json",
            "x-request-id": "abc123",
            "accept": "*/*",
        }
        result = _sanitize_headers(headers)
        assert result == headers

    def test_case_insensitive_redaction(self):
        headers = {
            "Authorization": "Bearer secret",
            "X-API-KEY": "my-key",
            "COOKIE": "session=abc",
        }
        result = _sanitize_headers(headers)
        for k in headers:
            assert result[k] == "***REDACTED***", f"Expected redaction for {k}"

    def test_empty_headers_returns_empty(self):
        assert _sanitize_headers({}) == {}

    def test_does_not_mutate_original(self):
        headers = {"authorization": "secret", "content-type": "application/json"}
        original = dict(headers)
        _sanitize_headers(headers)
        assert headers == original


# ---------------------------------------------------------------------------
# upload() — file system guards
# ---------------------------------------------------------------------------

class TestUploadFilesystemGuards:

    @pytest.mark.asyncio
    async def test_missing_data_json_raises(self, tmp_path):
        client = _make_client()
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        async with client:
            with pytest.raises(FileNotFoundError, match="data.json not found"):
                await client.upload(empty_dir)

    @pytest.mark.asyncio
    async def test_missing_session_dir_raises(self, tmp_path):
        client = _make_client()
        nonexistent = tmp_path / "ghost"
        async with client:
            with pytest.raises(FileNotFoundError):
                await client.upload(nonexistent)


# ---------------------------------------------------------------------------
# upload() — successful request
# ---------------------------------------------------------------------------

class TestUploadSuccess:

    @pytest.mark.asyncio
    async def test_returns_session_id_and_status(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, step_count=5, source="s-001")
        server_response = {"session_id": "s-001", "step_count": 5, "status": "accepted"}
        cm, _ = _mock_response(200, body=server_response)

        client = _make_client()
        with patch("aiohttp.ClientSession.request", return_value=cm):
            async with client:
                result = await client.upload(session_dir)

        assert result["session_id"] == "s-001"
        assert result["status"] == "accepted"
        assert result["step_count"] == 5

    @pytest.mark.asyncio
    async def test_on_progress_called(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, step_count=2)
        server_response = {"session_id": "x", "step_count": 2, "status": "accepted"}
        cm, _ = _mock_response(200, body=server_response)

        progress_messages: list[str] = []
        client = _make_client()
        with patch("aiohttp.ClientSession.request", return_value=cm):
            async with client:
                await client.upload(session_dir, on_progress=progress_messages.append)

        assert any("Preparing" in m for m in progress_messages)
        assert any("complete" in m.lower() for m in progress_messages)

    @pytest.mark.asyncio
    async def test_posts_to_correct_path(self, tmp_path):
        session_dir = _make_session_dir(tmp_path)
        server_response = {"session_id": "s", "step_count": 3, "status": "accepted"}
        cm, _ = _mock_response(200, body=server_response)

        client = _make_client(endpoint="https://cloud.psr.ai")
        with patch("aiohttp.ClientSession.request", return_value=cm) as mock_req:
            async with client:
                await client.upload(session_dir)

        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "/v1/sessions" in call_args[0][1]


# ---------------------------------------------------------------------------
# upload() — 429 rate limit with backoff
# ---------------------------------------------------------------------------

class TestUpload429Backoff:

    @pytest.mark.asyncio
    async def test_retries_after_429_and_succeeds(self, tmp_path):
        """First call returns 429, second call succeeds."""
        session_dir = _make_session_dir(tmp_path)
        server_response = {"session_id": "s-retry", "step_count": 3, "status": "accepted"}

        rate_limited_cm, _ = _mock_response(429, headers={"Retry-After": "0"})
        success_cm, _ = _mock_response(200, body=server_response)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return rate_limited_cm if call_count == 1 else success_cm

        client = _make_client()
        with patch("aiohttp.ClientSession.request", side_effect=side_effect):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                async with client:
                    result = await client.upload(session_dir)

        assert result["session_id"] == "s-retry"
        assert call_count == 2
        mock_sleep.assert_called()

    @pytest.mark.asyncio
    async def test_429_respects_retry_after_header(self, tmp_path):
        """Backoff wait must be at least the Retry-After value."""
        session_dir = _make_session_dir(tmp_path)
        server_response = {"session_id": "s", "step_count": 3, "status": "accepted"}

        rate_limited_cm, _ = _mock_response(429, headers={"Retry-After": "5"})
        success_cm, _ = _mock_response(200, body=server_response)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return rate_limited_cm if call_count == 1 else success_cm

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        client = _make_client()
        with patch("aiohttp.ClientSession.request", side_effect=side_effect):
            with patch("psr_ai.ee.client.asyncio.sleep", side_effect=fake_sleep):
                async with client:
                    await client.upload(session_dir)

        assert sleep_calls, "Expected at least one sleep call after 429"
        assert sleep_calls[0] >= 5.0, f"Sleep was {sleep_calls[0]}, expected ≥ 5.0 (Retry-After)"


# ---------------------------------------------------------------------------
# upload() — 500 server error with backoff
# ---------------------------------------------------------------------------

class TestUpload500Backoff:

    @pytest.mark.asyncio
    async def test_retries_after_500_and_succeeds(self, tmp_path):
        """First two calls return 500, third succeeds."""
        session_dir = _make_session_dir(tmp_path)
        server_response = {"session_id": "s-500", "step_count": 3, "status": "accepted"}

        error_cm, _ = _mock_response(500)
        success_cm, _ = _mock_response(200, body=server_response)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return error_cm if call_count < 3 else success_cm

        client = _make_client()
        with patch("aiohttp.ClientSession.request", side_effect=side_effect):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                async with client:
                    result = await client.upload(session_dir)

        assert result["session_id"] == "s-500"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises_runtime_error(self, tmp_path):
        """All retries raise ClientConnectionError — must raise RuntimeError after MAX_RETRIES."""
        import aiohttp as _aiohttp
        session_dir = _make_session_dir(tmp_path)

        client = _make_client()
        with patch(
            "aiohttp.ClientSession.request",
            side_effect=_aiohttp.ClientConnectionError("connection refused"),
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                async with client:
                    with pytest.raises(RuntimeError, match="failed after"):
                        await client.upload(session_dir)


# ---------------------------------------------------------------------------
# Security: no token in logs
# ---------------------------------------------------------------------------

class TestNoTokenInLogs:

    @pytest.mark.asyncio
    async def test_auth_token_never_appears_in_log_output(self, tmp_path, caplog):
        """The auth token must not appear in any log record at any level."""
        session_dir = _make_session_dir(tmp_path)
        secret_token = "tok-super-secret-12345"
        server_response = {"session_id": "s", "step_count": 3, "status": "accepted"}
        cm, _ = _mock_response(200, body=server_response)

        client = _make_client(token=secret_token)
        with patch("aiohttp.ClientSession.request", return_value=cm):
            with caplog.at_level(logging.DEBUG, logger="psr_ai.ee.client"):
                async with client:
                    await client.upload(session_dir)

        for record in caplog.records:
            assert secret_token not in record.getMessage(), (
                f"Token leaked in log: {record.getMessage()!r}"
            )

    @pytest.mark.asyncio
    async def test_sanitize_headers_called_pattern(self):
        """_sanitize_headers must redact x-psr-token."""
        headers = {"x-psr-token": "tok-secret", "x-psr-team": "acme"}
        result = _sanitize_headers(headers)
        assert result["x-psr-token"] == "***REDACTED***"
        assert result["x-psr-team"] == "acme"


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------

class TestContextManagerLifecycle:

    @pytest.mark.asyncio
    async def test_session_closed_on_exit(self):
        client = _make_client()
        async with client:
            assert client._session is not None
        assert client._session is None

    @pytest.mark.asyncio
    async def test_request_without_context_manager_raises(self, tmp_path):
        """Calling _request without entering the context manager must raise."""
        client = _make_client()
        with pytest.raises(RuntimeError, match="async context manager"):
            await client._request("GET", "/test")
