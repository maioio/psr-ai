"""Enterprise async client for streaming session packages to PSR.ai Cloud."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Any, Callable

import aiohttp

# --- Sensitive header sanitization (must appear before any logging call) ---

_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "x-auth-token",
    "x-session-token",
    "x-psr-token",
})


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: "***REDACTED***" if k.lower() in _SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }


# --- Rate limit configuration (all values from environment) ---

_RPM_LIMIT: int = int(os.getenv("PSR_CLOUD_RPM_LIMIT", "60"))
_MAX_CONCURRENT: int = max(1, _RPM_LIMIT // 10)
_BASE_BACKOFF_S: float = float(os.getenv("PSR_CLOUD_BASE_BACKOFF_S", "1.0"))
_MAX_BACKOFF_S: float = float(os.getenv("PSR_CLOUD_MAX_BACKOFF_S", "32.0"))
_MAX_RETRIES: int = int(os.getenv("PSR_CLOUD_MAX_RETRIES", "3"))
_REQUEST_TIMEOUT_S: float = float(os.getenv("PSR_CLOUD_TIMEOUT_S", "30.0"))

logger = logging.getLogger(__name__)


class PSRCloudClient:
    """Async client that streams session archives to a central telemetry endpoint.

    Usage:
        async with PSRCloudClient(endpoint="https://cloud.psr.ai", token="...") as client:
            result = await client.upload(session_dir)
    """

    def __init__(self, endpoint: str, token: str, team: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self.team = team
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async def __aenter__(self) -> PSRCloudClient:
        self._session = aiohttp.ClientSession(
            headers=self._build_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _build_auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"x-psr-token": self._token}
        if self.team:
            headers["x-psr-team"] = self.team
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{self.endpoint}{path}"
        backoff = _BASE_BACKOFF_S

        async with self._semaphore:
            for attempt in range(_MAX_RETRIES):
                try:
                    if self._session is None:
                        raise RuntimeError(
                            "PSRCloudClient must be used as an async context manager."
                        )
                    async with self._session.request(method, url, **kwargs) as resp:
                        if resp.status == 429:
                            retry_after = float(
                                resp.headers.get("Retry-After", backoff)
                            )
                            wait = min(retry_after, _MAX_BACKOFF_S) + random.uniform(0, 1)
                            logger.warning(
                                "Rate limited on %s %s — waiting %.1fs (attempt %d/%d)",
                                method, path, wait, attempt + 1, _MAX_RETRIES,
                            )
                            await asyncio.sleep(wait)
                            backoff = min(backoff * 2, _MAX_BACKOFF_S)
                            continue

                        if resp.status >= 500:
                            wait = min(backoff, _MAX_BACKOFF_S) + random.uniform(0, 0.5)
                            logger.warning(
                                "Server error %d on %s %s — retrying in %.1fs (attempt %d/%d)",
                                resp.status, method, path, wait, attempt + 1, _MAX_RETRIES,
                            )
                            await asyncio.sleep(wait)
                            backoff = min(backoff * 2, _MAX_BACKOFF_S)
                            continue

                        resp.raise_for_status()
                        return await resp.json()

                except aiohttp.ClientError as exc:
                    if attempt == _MAX_RETRIES - 1:
                        logger.error(
                            "Request failed after %d attempts: %s %s — %s",
                            _MAX_RETRIES, method, path, exc,
                        )
                        raise RuntimeError(
                            f"Upload to {url} failed after {_MAX_RETRIES} attempts: {exc}"
                        ) from exc
                    wait = min(backoff, _MAX_BACKOFF_S) + random.uniform(0, 0.5)
                    logger.warning(
                        "Network error on %s %s — retrying in %.1fs: %s",
                        method, path, wait, exc,
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, _MAX_BACKOFF_S)

        raise RuntimeError(f"Exhausted {_MAX_RETRIES} retries for {method} {path}")

    async def upload(
        self,
        session_dir: Path,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Stream a session output directory to the cloud endpoint.

        Args:
            session_dir: Path to the session output directory (contains data.json).
            on_progress: Optional callback for progress messages.

        Returns:
            Server response as a dict with at least a 'session_id' key.

        Raises:
            FileNotFoundError: If session_dir or data.json does not exist.
            RuntimeError: If the upload fails after all retries.
        """
        data_json = session_dir / "data.json"
        if not data_json.exists():
            raise FileNotFoundError(f"data.json not found in {session_dir}")

        payload = data_json.read_text(encoding="utf-8")
        step_count = __import__("json").loads(payload).get("step_count", 0)

        logger.debug(
            "Uploading session from %s (%d steps) → %s",
            session_dir, step_count, self.endpoint,
        )

        if on_progress:
            on_progress(f"Preparing {step_count} steps for upload...")

        result: dict = await self._request(
            "POST",
            "/v1/sessions",
            json={"payload": payload, "step_count": step_count},
        )

        logger.info(
            "Upload accepted: session_id=%s steps=%d",
            result.get("session_id"), step_count,
        )

        if on_progress:
            on_progress("Upload complete.")

        return result
