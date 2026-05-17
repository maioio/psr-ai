"""End-to-end automation pipeline for PSR.ai.

Chains the full lifecycle without manual triggers:
  1. Stop an active recording (sa stop equivalent)
  2. Analyze screenshots with a vision LLM (sa analyze, optional)
  3. Publish assets and push to GitHub with a new branch and Pull Request

The pipeline is designed for CI environments and autonomous agent use. Every
step emits structured events to an echo function so callers can pipe the output
to a terminal, a log file, or an LLM context window.

Typical usage:
    from psr_ai.core.pipeline import run_pipeline, PipelineConfig
    from pathlib import Path

    config = PipelineConfig(
        zip_path=Path("psr_output/psr_20240115_143000.zip"),
        project_root=Path("."),
        branch="docs/session-20240115",
        analyze=True,
        provider="anthropic",
        github_pr=True,
    )
    result = run_pipeline(config, echo_fn=print)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """All parameters for a single pipeline run.

    zip_path:     path to the PSR ZIP output from the recorder.
    project_root: root of the git repository.
    branch:       branch name to create and push to on GitHub.
                  If None, pushes to the current branch.
    create_branch: when True and branch is set, create the branch before commit.
    analyze:      run sa analyze (sends screenshots to the vision LLM).
    provider:     LLM provider for analyze step: "anthropic" or "openai".
    model:        LLM model override (None uses each provider's default).
    github_pr:    after pushing, open a Pull Request via the GitHub API.
                  Requires GITHUB_TOKEN and GITHUB_REPO env vars.
    pr_base:      base branch for the Pull Request (default: "main").
    dest_root:    root directory for published sessions.
    remote:       git remote name (default: "origin").
    commit_msg:   custom commit message (None uses timestamped default).
    no_gif:       skip GIF generation during the parse step.
    """
    zip_path: Path
    project_root: Path
    branch: str | None = None
    create_branch: bool = True
    analyze: bool = False
    provider: str = "anthropic"
    model: str | None = None
    github_pr: bool = False
    pr_base: str = "main"
    dest_root: Path | None = None
    remote: str = "origin"
    commit_msg: str | None = None
    no_gif: bool = False


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Outcome of a single run_pipeline() call."""
    config: PipelineConfig
    ran_at: str                    # ISO-8601 UTC
    success: bool
    output_dir: Path | None = None
    json_path: Path | None = None
    report_path: Path | None = None
    gif_path: Path | None = None
    enriched_steps: int = 0
    commit_sha: str = ""
    pr_url: str = ""
    error: str = ""
    steps: list[str] = field(default_factory=list)  # ordered log of executed steps

    def report(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"Pipeline result: {status}",
            f"  Ran at:        {self.ran_at}",
            f"  ZIP:           {self.config.zip_path}",
        ]
        if self.output_dir:
            lines.append(f"  Output dir:    {self.output_dir}")
        if self.report_path:
            lines.append(f"  Report:        {self.report_path}")
        if self.gif_path:
            lines.append(f"  GIF:           {self.gif_path}")
        if self.enriched_steps:
            lines.append(f"  AI steps:      {self.enriched_steps}")
        if self.commit_sha:
            lines.append(f"  Commit:        {self.commit_sha[:12]}")
        if self.pr_url:
            lines.append(f"  PR:            {self.pr_url}")
        if self.error:
            lines.append(f"  Error:         {self.error}")
        if self.steps:
            lines.append("  Steps executed:")
            for s in self.steps:
                lines.append(f"    {s}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    config: PipelineConfig,
    echo_fn: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Execute the full stop - analyze - publish pipeline.

    Never raises. All exceptions are caught, recorded in result.error, and
    result.success is set to False. Callers should check result.success.
    """
    _echo = echo_fn or (lambda _: None)
    ran_at = datetime.now(timezone.utc).isoformat()
    result = PipelineResult(config=config, ran_at=ran_at, success=False)

    try:
        _run(config, result, _echo)
        result.success = True
    except Exception as exc:
        result.error = str(exc)
        _echo(f"Pipeline error: {exc}")

    return result


def _run(
    config: PipelineConfig,
    result: PipelineResult,
    echo: Callable[[str], None],
) -> None:
    """Internal implementation: raises on any failure."""
    zip_path = Path(config.zip_path).resolve()
    project_root = Path(config.project_root).resolve()

    # Step 1: parse the ZIP into an output directory.
    echo(f"[1/4] Parsing: {zip_path.name}")
    output_dir, json_path, report_path, gif_path = _parse_step(zip_path, config, echo)
    result.output_dir = output_dir
    result.json_path = json_path
    result.report_path = report_path
    result.gif_path = gif_path
    result.steps.append(f"parse: {output_dir}")

    # Step 2: AI analysis (optional).
    if config.analyze:
        echo(f"[2/4] Analyzing with {config.provider} ({config.model or 'default model'})...")
        enriched = _analyze_step(output_dir, config, echo)
        result.enriched_steps = enriched
        result.steps.append(f"analyze: {enriched} steps enriched")
    else:
        echo("[2/4] Skipping analysis (analyze=False).")
        result.steps.append("analyze: skipped")

    # Step 3: publish assets and commit.
    echo("[3/4] Publishing assets...")
    sha = _publish_step(output_dir, project_root, config, echo)
    result.commit_sha = sha
    result.steps.append(f"publish: commit {sha[:12] if sha else 'n/a'}")

    # Step 4: open a Pull Request (optional).
    if config.github_pr and config.branch:
        echo("[4/4] Opening Pull Request...")
        pr_url = _open_pr(config, output_dir, echo)
        result.pr_url = pr_url
        result.steps.append(f"pr: {pr_url}")
    else:
        echo("[4/4] Skipping Pull Request creation.")
        result.steps.append("pr: skipped")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _parse_step(
    zip_path: Path,
    config: PipelineConfig,
    echo: Callable[[str], None],
) -> tuple[Path, Path, Path, Path | None]:
    """Parse the ZIP and render report.md + GIF. Returns (output_dir, json_path, report_path, gif_path)."""
    from psr_ai.core.parser import parse_session, generate_json_meta
    from psr_ai.core.generator import MarkdownGenerator, GifGenerator
    from psr_ai.utils.paths import default_output_dir

    session = parse_session(zip_path)
    output_dir = default_output_dir(zip_path)
    json_path = generate_json_meta(session, output_dir / "data.json")
    report_path = MarkdownGenerator(json_path).write(output_dir)
    echo(f"  Parsed {len(session.steps)} steps -> {output_dir.name}")

    gif_path: Path | None = None
    if not config.no_gif:
        try:
            gif_gen = GifGenerator(
                data_json_path=json_path,
                screenshots_dir=output_dir / "screenshots",
            )
            gif_path = gif_gen.write(output_dir)
            echo(f"  GIF: {gif_path.name}")
        except ImportError:
            echo("  GIF skipped: install psr-ai[gif] to enable GIF output.")
        except ValueError as exc:
            echo(f"  GIF skipped: {exc}")

    return output_dir, json_path, report_path, gif_path


def _analyze_step(
    output_dir: Path,
    config: PipelineConfig,
    echo: Callable[[str], None],
) -> int:
    """Run the AI analysis step. Returns count of enriched steps."""
    try:
        from psr_ai.core.analyzer import analyze_session
    except ImportError as exc:
        raise ImportError(
            "AI analysis requires the [ai] extra: pip install psr-ai[ai]"
        ) from exc

    return analyze_session(
        output_dir,
        provider=config.provider,
        model=config.model,
        regenerate=True,
        echo_fn=echo,
    )


def _publish_step(
    output_dir: Path,
    project_root: Path,
    config: PipelineConfig,
    echo: Callable[[str], None],
) -> str:
    """Copy assets and commit+push. Returns commit SHA or empty string."""
    from psr_ai.core.publisher import Publisher, PublishError

    dest_root = config.dest_root or (project_root / "docs" / "sessions")
    pub = Publisher(
        source_dir=output_dir,
        dest_root=dest_root,
        repo_root=project_root,
    )

    dest = pub.copy()
    echo(f"  Copied to: {dest}")

    if not pub.is_git_repo():
        echo("  Skipping git: not inside a git repository.")
        return ""

    try:
        sha = pub.commit_and_push(
            dest=dest,
            message=config.commit_msg,
            remote=config.remote,
            branch=config.branch,
            create_branch=config.create_branch and bool(config.branch),
        )
        echo(f"  Committed: {sha[:12]}")
        return sha
    except PublishError as exc:
        echo(f"  Git warning: {exc}")
        return ""


def _open_pr(
    config: PipelineConfig,
    output_dir: Path,
    echo: Callable[[str], None],
) -> str:
    """Open a GitHub Pull Request. Returns the PR HTML URL."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")

    if not token:
        raise ValueError(
            "GITHUB_TOKEN environment variable is required to open a Pull Request."
        )
    if not repo:
        raise ValueError(
            "GITHUB_REPO environment variable is required (format: owner/repo)."
        )

    branch = config.branch
    if not branch:
        raise ValueError("A branch name is required to open a Pull Request.")

    session_name = output_dir.name
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    payload = {
        "title": f"docs: add PSR session {session_name} [{ts}]",
        "body": (
            f"Automated Pull Request created by PSR.ai pipeline.\n\n"
            f"Session: `{session_name}`\n"
            f"Branch: `{branch}`\n"
            f"Base: `{config.pr_base}`\n\n"
            f"Generated by `sa pipeline` at {datetime.now(timezone.utc).isoformat()}."
        ),
        "head": branch,
        "base": config.pr_base,
        "draft": False,
    }

    url = f"https://api.github.com/repos/{repo}/pulls"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
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
            pr_url = result.get("html_url", "")
            echo(f"  PR opened: {pr_url}")
            return pr_url
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"GitHub API error {exc.code}: {body}") from exc
