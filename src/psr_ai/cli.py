"""CLI entrypoint: defines the `sa` command group and its subcommands."""

from __future__ import annotations

from pathlib import Path

import click

from psr_ai import __version__
from psr_ai.core.recorder import PSRNotFoundError, Recorder
from psr_ai.core.parser import parse_session, generate_json_meta
from psr_ai.core.generator import GifGenerator, MarkdownGenerator, generate_markdown, generate_gif
from psr_ai.core.publisher import Publisher, PublishError, publish_via_github_api
from psr_ai.utils.paths import default_output_dir


@click.group()
@click.version_option(__version__, prog_name="sa")
def main() -> None:
    """PSR.ai - turn Windows Steps Recorder output into AI-ready documentation."""


# ------------------------------------------------------------------
# sa start
# ------------------------------------------------------------------

@main.command()
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Path for the PSR output ZIP. Defaults to a timestamped file in psr_output/.",
)
@click.option(
    "--maxsc",
    default=25,
    show_default=True,
    help="Maximum number of screenshots PSR will capture.",
)
def start(output: str | None, maxsc: int) -> None:
    """Start a new PSR recording session.

    PSR runs in the background - your terminal is free immediately.
    Run `sa stop` when you are done to save the recording.
    """
    try:
        recorder = Recorder()
    except PSRNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    out_path = recorder.start_recording(output_path=output, max_screenshots=maxsc)
    click.echo("Recording started.")
    click.echo(f"  Output: {out_path}")
    click.echo(f"  Max screenshots: {maxsc}")
    click.echo("Run `sa stop` when finished.")


# ------------------------------------------------------------------
# sa stop
# ------------------------------------------------------------------

@main.command()
@click.option(
    "--no-gif",
    "skip_gif",
    is_flag=True,
    default=False,
    help="Skip GIF generation and produce only data.json and report.md.",
)
def stop(skip_gif: bool) -> None:
    """Stop the active PSR recording session and save the ZIP.

    Waits for PSR to finish writing the file before returning.
    After saving, automatically parses the ZIP, renders report.md,
    and builds action_replay.gif (pass --no-gif to skip the GIF).
    """
    try:
        recorder = Recorder()
    except PSRNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    zip_path = recorder.stop_recording()

    if zip_path is not None and zip_path.exists():
        size_kb = zip_path.stat().st_size // 1024
        click.echo("Recording stopped.")
        click.echo(f"  File: {zip_path}")
        click.echo(f"  Size: {size_kb} KB")

        # Auto-parse: extract steps, dump data.json, render report.md, build GIF.
        try:
            session = parse_session(zip_path)
            output_dir = default_output_dir(zip_path)
            json_path = generate_json_meta(session, output_dir / "data.json")
            report_path = MarkdownGenerator(json_path).write(output_dir)
            screenshots_dir = output_dir / "screenshots"

            click.echo(f"  Parsed:      {len(session.steps)} steps extracted")
            click.echo(f"  Data:        {json_path}")
            click.echo(f"  Screenshots: {screenshots_dir}")
            click.echo(f"  Report:      {report_path}")

            if not skip_gif:
                _build_gif_for_stop(session, output_dir, json_path)

        except Exception as exc:
            click.echo(
                f"  Warning: auto-parse failed ({exc}). The ZIP was saved successfully.",
                err=True,
            )
            click.echo(f"  Run `sa pub {zip_path}` to generate documentation manually.")
    else:
        click.echo("Recording stopped.")
        click.echo("  Note: output file not found - PSR may have exited before saving.")


def _build_gif_for_stop(session, output_dir: Path, json_path: Path) -> None:
    """Build action_replay.gif and print status. Called only from sa stop."""
    try:
        gif_gen = GifGenerator(
            data_json_path=json_path,
            screenshots_dir=output_dir / "screenshots",
        )
        gif_path = gif_gen.write(output_dir)
        click.echo(f"  GIF:         {gif_path}")
    except ImportError:
        click.echo(
            "  GIF skipped: Pillow not installed. Run: pip install psr-ai[gif]",
            err=True,
        )
    except ValueError as exc:
        # No frames found - session had no screenshots. Non-fatal.
        click.echo(f"  GIF skipped: {exc}", err=True)


# ------------------------------------------------------------------
# sa status
# ------------------------------------------------------------------

@main.command()
def status() -> None:
    """Check whether a PSR recording is currently active."""
    try:
        recorder = Recorder()
    except PSRNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    active = recorder.is_recording_active()
    if active:
        click.echo("Status: RECORDING")
        click.echo("  psr.exe is running. Use `sa stop` to end the session.")
    else:
        click.echo("Status: IDLE")
        click.echo("  No active recording. Use `sa start` to begin.")


# ------------------------------------------------------------------
# sa pub
# ------------------------------------------------------------------

@main.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--dest", "-d",
    default=None,
    type=click.Path(),
    help="Root directory for published sessions. Defaults to ./docs/sessions/.",
)
@click.option(
    "--git",
    "use_git",
    is_flag=True,
    default=False,
    help="Commit and push the published assets using Git.",
)
@click.option(
    "--message", "-m",
    default=None,
    help="Custom Git commit message. Defaults to an automated timestamped message.",
)
@click.option(
    "--remote",
    default="origin",
    show_default=True,
    help="Git remote to push to.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to push to. Defaults to the current branch.",
)
@click.option(
    "--github-api",
    "use_github_api",
    is_flag=True,
    default=False,
    help=(
        "Push report.md via the GitHub Contents API instead of local Git. "
        "Requires GITHUB_TOKEN and GITHUB_REPO environment variables."
    ),
)
def pub(
    source_dir: str,
    dest: str | None,
    use_git: bool,
    message: str | None,
    remote: str,
    branch: str | None,
    use_github_api: bool,
) -> None:
    """Publish a session output directory to docs/ and optionally push to GitHub.

    SOURCE_DIR is the output directory produced by `sa stop`
    (the folder containing report.md, data.json, action_replay.gif, screenshots/).

    Examples:
      sa pub psr_output/psr_20240115_143000_output
      sa pub psr_output/psr_20240115_143000_output --git
      sa pub psr_output/psr_20240115_143000_output --git --message "docs: weekly sync"
      sa pub psr_output/psr_20240115_143000_output --github-api
    """
    src = Path(source_dir)
    dest_root = Path(dest) if dest else None

    publisher = Publisher(source_dir=src, dest_root=dest_root)

    # Step 1: copy assets.
    click.echo(f"Publishing: {src.name}")
    try:
        copied_to = publisher.copy()
    except OSError as exc:
        click.echo(f"Error copying assets: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"  Copied to:   {copied_to}")
    _report_copied_assets(copied_to)

    # Step 2: GitHub Contents API push (no local git needed).
    if use_github_api:
        click.echo("  Pushing via GitHub Contents API...")
        try:
            html_url = publish_via_github_api(src)
            click.echo(f"  Published:   {html_url}")
        except PublishError as exc:
            click.echo(f"  GitHub API error: {exc}", err=True)
            raise SystemExit(1)
        return

    # Step 3: Local git commit + push.
    if use_git:
        if not publisher.is_git_repo():
            click.echo(
                "  Git error: not inside a Git repository. "
                "Run `git init` first or use --github-api.",
                err=True,
            )
            raise SystemExit(1)
        click.echo("  Committing...")
        try:
            sha = publisher.commit_and_push(
                dest=copied_to,
                message=message,
                remote=remote,
                branch=branch,
            )
            click.echo(f"  Committed:   {sha[:12]}")
            click.echo(f"  Pushed to:   {remote}" + (f"/{branch}" if branch else ""))
        except PublishError as exc:
            click.echo(f"  Git error: {exc}", err=True)
            raise SystemExit(1)
        return

    click.echo("Done. Use --git to commit and push, or --github-api for the Contents API.")


def _report_copied_assets(dest: Path) -> None:
    """Print which files were copied to the destination directory."""
    from psr_ai.core.publisher import _ASSET_FILES, _ASSET_DIRS
    for name in _ASSET_FILES:
        if (dest / name).exists():
            click.echo(f"  + {name}")
    for name in _ASSET_DIRS:
        if (dest / name).is_dir():
            count = sum(1 for _ in (dest / name).iterdir())
            click.echo(f"  + {name}/ ({count} files)")
