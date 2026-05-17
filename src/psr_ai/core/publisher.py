"""Publisher: copies session assets and optionally commits + pushes via Git."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Raised when a publish operation cannot complete."""


# ---------------------------------------------------------------------------
# Asset manifest: the files and directories copied per session
# ---------------------------------------------------------------------------

_ASSET_FILES = ("report.md", "data.json", "action_replay.gif")
_ASSET_DIRS  = ("screenshots",)


class Publisher:
    """Copies session assets from an output directory and optionally commits.

    Typical usage:
        pub = Publisher(source_dir=output_dir, dest_root=Path("docs/sessions"))
        dest = pub.copy()
        pub.commit_and_push(dest, message="docs: add session 2024-01-15")

    source_dir: the output directory produced by the generator pipeline
                (contains report.md, data.json, action_replay.gif, screenshots/).
    dest_root:  parent directory under which a per-session subdirectory is created.
                Defaults to ./docs/sessions/ relative to the current working directory.
    repo_root:  root of the Git repository for git commands.
                Defaults to the current working directory.
    """

    def __init__(
        self,
        source_dir: Path,
        dest_root: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.dest_root = Path(dest_root) if dest_root else Path.cwd() / "docs" / "sessions"
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------

    def copy(self) -> Path:
        """Copy all session assets from source_dir into dest_root/<session_name>/.

        Returns the destination directory path.
        Skips files that do not exist (e.g. GIF was skipped by --no-gif).
        """
        dest = self.dest_root / self.source_dir.name
        dest.mkdir(parents=True, exist_ok=True)

        for filename in _ASSET_FILES:
            src_file = self.source_dir / filename
            if src_file.exists():
                shutil.copy2(src_file, dest / filename)
                logger.debug("Copied: %s -> %s", src_file, dest / filename)

        for dirname in _ASSET_DIRS:
            src_dir = self.source_dir / dirname
            if src_dir.exists():
                dst_dir = dest / dirname
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                logger.debug("Copied dir: %s -> %s", src_dir, dst_dir)

        return dest

    # ------------------------------------------------------------------
    # Git automation
    # ------------------------------------------------------------------

    def current_branch(self) -> str:
        """Return the name of the currently checked-out branch."""
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()

    def create_branch(self, branch: str) -> None:
        """Create and check out a new local branch.

        Raises PublishError if the branch already exists locally.
        """
        self._git("checkout", "-b", branch)

    def branch_exists_remote(self, branch: str, remote: str = "origin") -> bool:
        """Return True if the branch already exists on the given remote."""
        try:
            result = self._git(
                "ls-remote", "--heads", remote,
                f"refs/heads/{branch}",
            )
            return bool(result.stdout.strip())
        except PublishError:
            return False

    def commit_and_push(
        self,
        dest: Path,
        message: str | None = None,
        remote: str = "origin",
        branch: str | None = None,
        create_branch: bool = False,
    ) -> str:
        """Stage the destination directory, commit, and push.

        dest:          the directory returned by copy() - passed to git add.
        message:       commit message. Defaults to a timestamped automated message.
        remote:        git remote name (default: origin).
        branch:        branch to push to. Defaults to the current branch.
        create_branch: when True and branch is set, create the branch locally
                       before pushing. Raises PublishError if the branch already
                       exists locally.

        Returns the commit SHA on success.
        Raises PublishError with the captured stderr if any git command fails.
        """
        if message is None:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session_name = dest.name
            message = f"docs: add PSR session {session_name} [{ts}]"

        if create_branch and branch:
            self.create_branch(branch)

        self._git("add", str(dest.relative_to(self.repo_root)))
        self._git("commit", "-m", message)

        push_args = ["push"]
        if create_branch and branch:
            # -u / --set-upstream must come before remote and branch name.
            push_args.extend(["-u", remote, branch])
        elif branch:
            push_args.extend([remote, branch])
        else:
            push_args.append(remote)
        self._git(*push_args)

        # Return the commit SHA so the caller can log or display it.
        result = self._git("rev-parse", "HEAD")
        return result.stdout.strip()

    def git_status(self) -> str:
        """Return the output of `git status --short` in the repo root."""
        result = self._git("status", "--short")
        return result.stdout

    def is_git_repo(self) -> bool:
        """Return True if repo_root is inside a Git repository."""
        try:
            self._git("rev-parse", "--git-dir")
            return True
        except PublishError:
            return False

    # ------------------------------------------------------------------
    # Internal git runner
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """Run a git command in repo_root and return the CompletedProcess.

        Captures both stdout and stderr. Raises PublishError on non-zero exit.
        """
        cmd = ["git", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PublishError(
                "git executable not found. Install Git and ensure it is on PATH."
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise PublishError(
                f"git {args[0]} failed (exit {result.returncode}): {stderr}"
            )
        return result


# ---------------------------------------------------------------------------
# GitHub Contents API push (kept as a standalone function for sa pub --publish)
# ---------------------------------------------------------------------------

def publish_via_github_api(out_dir: Path) -> str:
    """Push report.md to a GitHub repository via the Contents API.

    Required environment variables:
      GITHUB_TOKEN  - personal access token with repo write scope
      GITHUB_REPO   - owner/repo (e.g. acme/my-project)
      GITHUB_BRANCH - target branch (defaults to main)

    Returns the GitHub html_url of the committed file.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo  = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH", "main")

    if not token:
        raise PublishError("GITHUB_TOKEN environment variable is not set.")
    if not repo:
        raise PublishError("GITHUB_REPO environment variable is not set (format: owner/repo).")

    md_path = out_dir / "report.md"
    if not md_path.exists():
        raise PublishError(f"report.md not found in {out_dir}. Run the generator first.")

    import base64
    content_b64 = base64.b64encode(md_path.read_bytes()).decode("ascii")

    api_path = f"docs/sessions/{out_dir.name}/report.md"
    url = f"https://api.github.com/repos/{repo}/contents/{api_path}"

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
    """Return the blob SHA if the file already exists on GitHub, else None."""
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


# ---------------------------------------------------------------------------
# Backward-compatible wrapper used by older call sites
# ---------------------------------------------------------------------------

def publish(out_dir: Path, target: str = "local") -> str:
    """Thin wrapper kept for backward compatibility.

    target "local": copy assets to ./docs/sessions/ via Publisher.copy().
    target "github": push report.md via the GitHub Contents API.
    """
    if target == "github":
        return publish_via_github_api(out_dir)
    pub = Publisher(source_dir=out_dir)
    dest = pub.copy()
    return str(dest)
