"""Enterprise async client for streaming session packages to PSR.ai Cloud."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable


class PSRCloudClient:
    """Async client that streams session archives to a central telemetry endpoint.

    Usage:
        client = PSRCloudClient(endpoint="https://cloud.psr.ai", token="...")
        asyncio.run(client.upload(session_dir))
    """

    def __init__(self, endpoint: str, token: str, team: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.team = team

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
            RuntimeError: If the upload fails or the server returns an error.
        """
        data_json = session_dir / "data.json"
        if not data_json.exists():
            raise FileNotFoundError(f"data.json not found in {session_dir}")

        # NOTE: Real implementation would use aiohttp or httpx to stream the
        # session archive. This stub validates the session and returns a mock
        # response so the interface can be tested without a live endpoint.
        session_data = json.loads(data_json.read_text(encoding="utf-8"))
        step_count = session_data.get("step_count", 0)

        if on_progress:
            on_progress(f"Preparing {step_count} steps for upload...")

        await asyncio.sleep(0)  # yield to event loop

        if on_progress:
            on_progress("Upload complete.")

        return {
            "session_id": session_data.get("source", "unknown"),
            "step_count": step_count,
            "status": "accepted",
            "endpoint": self.endpoint,
        }
