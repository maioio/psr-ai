"""Publishes generated documentation to a local docs/ folder or a GitHub repository."""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
import urllib.error
from pathlib import Path


class PublishError(RuntimeError):
    """Raised when a publish operation cannot complete."""


def publish(out_dir: Path, target: str = "local") -> str:
    """Dispatch to local or GitHub publish based on target.

    target: "local" copies docs into a docs/ subfolder of the current project.
             "github" pushes via the GitHub Contents API (requires GITHUB_TOKEN).
    """
    if target == "github":
        return _publish_github(out_dir)
    return _publish_local(out_dir)


def _publish_local(out_dir: Path) -> str:
    """Copy the generated output into ./docs/sessions/ relative to cwd."""
    dest = Path.cwd() / "docs" / "sessions" / out_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    for item in out_dir.iterdir():
        dst_item = dest / item.name
        if item.is_dir():
            if dst_item.exists():
                shutil.rmtree(dst_item)
            shutil.copytree(item, dst_item)
        else:
            shutil.copy2(item, dst_item)
    return str(dest)


def _publish_github(out_dir: Path) -> str:
    """Push session.md to a GitHub repository via the Contents API.

    Required environment variables:
      GITHUB_TOKEN  - personal access token with repo write scope
      GITHUB_REPO   - owner/repo (e.g. acme/my-project)
      GITHUB_BRANCH - target branch (defaults to main)
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH", "main")

    if not token:
        raise PublishError("GITHUB_TOKEN environment variable is not set.")
    if not repo:
        raise PublishError("GITHUB_REPO environment variable is not set (format: owner/repo).")

    md_path = out_dir / "session.md"
    if not md_path.exists():
        raise PublishError(f"session.md not found in {out_dir}. Run `sa pub` first.")

    import base64
    content_b64 = base64.b64encode(md_path.read_bytes()).decode("ascii")

    api_path = f"docs/sessions/{out_dir.name}/session.md"
    url = f"https://api.github.com/repos/{repo}/contents/{api_path}"

    # Check if the file already exists to get its SHA (needed for updates).
    sha: str | None = _get_existing_sha(url, token, branch)

    payload: dict = {
        "message": f"docs: add PSR session {out_dir.name}",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result.get("content", {}).get("html_url", url)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PublishError(f"GitHub API error {exc.code}: {body}") from exc


def _get_existing_sha(url: str, token: str, branch: str) -> str | None:
    """Return the blob SHA if the file exists on GitHub, or None if it does not."""
    req = urllib.request.Request(
        f"{url}?ref={branch}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data.get("sha")
    except urllib.error.HTTPError:
        return None
