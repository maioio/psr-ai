"""CLI entrypoint: defines the `sa` command group and its subcommands."""

from __future__ import annotations

from pathlib import Path

import click

from psr_ai import __version__
from psr_ai.core.recorder import PSRNotFoundError, Recorder
from psr_ai.core.parser import parse_session, generate_json_meta
from psr_ai.core.generator import GifGenerator, MarkdownGenerator, generate_markdown, generate_gif
from psr_ai.core.reviewer import run_review_session
from psr_ai.core.publisher import Publisher, PublishError, publish_via_github_api
from psr_ai.utils.paths import default_output_dir

# analyzer is imported lazily inside sa analyze to avoid requiring the ai extra
# for users who never use the command.


def _echo(msg: str) -> None:
    """click.echo with a safe fallback for terminals that cannot encode Unicode paths."""
    try:
        click.echo(msg)
    except UnicodeEncodeError:
        click.echo(msg.encode("ascii", errors="replace").decode("ascii"))


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
    _echo(f"  Output: {out_path}")
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
        _echo(f"  File: {zip_path}")
        click.echo(f"  Size: {size_kb} KB")

        # Auto-parse: extract steps, dump data.json, render report.md, build GIF.
        try:
            session = parse_session(zip_path)
            output_dir = default_output_dir(zip_path)
            json_path = generate_json_meta(session, output_dir / "data.json")
            report_path = MarkdownGenerator(json_path).write(output_dir)
            screenshots_dir = output_dir / "screenshots"

            click.echo(f"  Parsed:      {len(session.steps)} steps extracted")
            _echo(f"  Data:        {json_path}")
            _echo(f"  Screenshots: {screenshots_dir}")
            _echo(f"  Report:      {report_path}")

            if not skip_gif:
                _build_gif_for_stop(session, output_dir, json_path)

        except Exception as exc:
            click.echo(
                f"  Warning: auto-parse failed ({exc}). The ZIP was saved successfully.",
                err=True,
            )
            _echo(f"  Run `sa pub {zip_path}` to generate documentation manually.")
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
        _echo(f"  GIF:         {gif_path}")
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
# sa review
# ------------------------------------------------------------------

@main.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--no-regen",
    "skip_regen",
    is_flag=True,
    default=False,
    help="Skip re-generating report.md after the review.",
)
def review(source_dir: str, skip_regen: bool) -> None:
    """Interactively review session steps and drop sensitive data.

    SOURCE_DIR is the output directory produced by `sa stop`
    (must contain data.json and a screenshots/ subdirectory).

    For each step you will see the timestamp, program name, and action text.
    Press Enter or K to keep the step, D to drop it.
    Dropped steps are immediately purged from data.json and their PNG
    screenshots are deleted from disk before any report or GIF is generated.

    Example:
      sa review psr_output\\psr_20240115_143000_output
    """
    try:
        kept, dropped = run_review_session(
            Path(source_dir),
            regenerate=not skip_regen,
        )
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"\nSummary: {kept} steps kept, {dropped} steps dropped.")
    if dropped > 0:
        click.echo(
            "Run `sa pub <source_dir>` to publish the privacy-reviewed session."
        )


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

    _echo(f"  Copied to:   {copied_to}")
    _report_copied_assets(copied_to)

    # Step 2: GitHub Contents API push (no local git needed).
    if use_github_api:
        click.echo("  Pushing via GitHub Contents API...")
        try:
            html_url = publish_via_github_api(src)
            _echo(f"  Published:   {html_url}")
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


# ------------------------------------------------------------------
# sa analyze
# ------------------------------------------------------------------

@main.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--provider", "-p",
    default="anthropic",
    show_default=True,
    type=click.Choice(["anthropic", "openai"]),
    help="Vision LLM provider to use.",
)
@click.option(
    "--model", "-m",
    default=None,
    help=(
        "Model name override. "
        "Defaults: anthropic=claude-sonnet-4-6, openai=gpt-4o."
    ),
)
@click.option(
    "--no-regen",
    "skip_regen",
    is_flag=True,
    default=False,
    help="Skip re-generating report.md after analysis.",
)
def analyze(source_dir: str, provider: str, model: str | None, skip_regen: bool) -> None:
    """Send session screenshots to a vision LLM and write AI descriptions to data.json.

    SOURCE_DIR is the output directory produced by `sa stop`
    (must contain data.json and a screenshots/ subdirectory).

    Each screenshot is sent to the chosen vision model. The response is written
    back as 'ai_description' on the corresponding step in data.json. Steps
    without a screenshot are skipped.

    Requires the ai extra:
      pip install psr-ai[ai]

    Environment variables required by each provider:
      anthropic : ANTHROPIC_API_KEY
      openai    : OPENAI_API_KEY

    Examples:
      sa analyze psr_output\\psr_20240115_143000_output
      sa analyze psr_output\\psr_20240115_143000_output --provider openai
      sa analyze psr_output\\psr_20240115_143000_output --model claude-opus-4-7
    """
    try:
        from psr_ai.core.analyzer import analyze_session
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    try:
        enriched = analyze_session(
            Path(source_dir),
            provider=provider,
            model=model,
            regenerate=not skip_regen,
            echo_fn=_echo,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"\nDone. {enriched} steps enriched with AI descriptions.")


# ------------------------------------------------------------------
# sa agent
# ------------------------------------------------------------------

@main.command()
@click.argument("task")
@click.option(
    "--screenshot", "-s",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Initial screenshot to use as starting context instead of a live capture.",
)
@click.option(
    "--max-steps",
    default=20,
    show_default=True,
    help="Hard stop: maximum number of actions the agent can take.",
)
@click.option(
    "--provider", "-p",
    default="anthropic",
    show_default=True,
    type=click.Choice(["anthropic", "openai"]),
    help="Vision LLM provider.",
)
@click.option(
    "--model", "-m",
    default=None,
    help="Model override. Defaults: anthropic=claude-sonnet-4-6, openai=gpt-4o.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help=(
        "Safe mode (default ON): show planned actions without executing them. "
        "Pass --no-dry-run to allow execution."
    ),
)
@click.option(
    "--ask/--no-ask",
    "ask_before_action",
    default=True,
    show_default=True,
    help="Prompt for approval before every action (default ON).",
)
@click.option(
    "--output-dir", "-o",
    default=None,
    type=click.Path(),
    help="Save the agent session log to this directory.",
)
def agent(
    task: str,
    screenshot: str | None,
    max_steps: int,
    provider: str,
    model: str | None,
    dry_run: bool,
    ask_before_action: bool,
    output_dir: str | None,
) -> None:
    """Let Claude autonomously diagnose and fix a problem on the desktop.

    TASK is a natural-language description of the problem or goal.

    In dry-run mode (default), Claude plans each action and shows it to you
    but does NOT execute it. Pass --no-dry-run to enable actual execution.

    Examples:
      sa agent "The app crashes when I click Save - find out why"
      sa agent "Open Chrome and go to github.com" --no-dry-run --no-ask
      sa agent "Fix the error shown" --screenshot error.png --dry-run
      sa agent "Close all open windows" --no-dry-run --provider openai

    Requires:
      pip install psr-ai[agent]
      ANTHROPIC_API_KEY or OPENAI_API_KEY (depending on --provider)
    """
    try:
        from psr_ai.core.agent import AgentConfig, run_agent
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if not dry_run and not ask_before_action:
        click.echo(
            "WARNING: Running in fully autonomous mode (--no-dry-run --no-ask). "
            "Claude will execute actions without confirmation.",
            err=True,
        )

    config = AgentConfig(
        task=task,
        max_steps=max_steps,
        provider=provider,
        model=model,
        dry_run=dry_run,
        ask_before_action=ask_before_action,
        output_dir=Path(output_dir) if output_dir else None,
        initial_screenshot=Path(screenshot) if screenshot else None,
    )

    try:
        result = run_agent(config, echo_fn=_echo)
    except (ValueError, ImportError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    _echo(result.report())
    raise SystemExit(0 if result.success else 1)


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


# ------------------------------------------------------------------
# sa heal
# ------------------------------------------------------------------

@main.command()
@click.option(
    "--session", "-s",
    default=None,
    type=click.Path(file_okay=False),
    help=(
        "PSR.ai session output directory to use as failure context. "
        "When provided, the last recorded step is shown alongside test failures."
    ),
)
@click.option(
    "--filter", "-k",
    "test_filter",
    default=None,
    help="Pytest -k expression to restrict which tests are run.",
)
@click.option(
    "--project", "-p",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Project root directory (where pyproject.toml lives). Defaults to cwd.",
)
def heal(
    session: str | None,
    test_filter: str | None,
    project: str | None,
) -> None:
    """Run the test suite and report structured diagnostics.

    Reads the latest PSR.ai session data (when --session is supplied) to
    correlate recorded interactions with test failures.

    Examples:
      sa heal
      sa heal --session psr_output/psr_20240115_143000_output
      sa heal --filter parser --project /path/to/repo
    """
    from psr_ai.core.healer import heal as _heal

    project_root = Path(project) if project else Path.cwd()
    session_dir = Path(session) if session else None

    pytest_args: list[str] = []
    if test_filter:
        pytest_args.extend(["-k", test_filter])

    click.echo("Running test suite...")
    result = _heal(
        project_root=project_root,
        session_dir=session_dir,
        pytest_args=pytest_args,
    )

    _echo(result.report())
    raise SystemExit(0 if result.all_green else 1)


# ------------------------------------------------------------------
# sa testgen
# ------------------------------------------------------------------

@main.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output path for the generated test file. Defaults to <source_dir>/generated_test.py.",
)
@click.option(
    "--framework", "-f",
    default="playwright",
    show_default=True,
    type=click.Choice(["playwright"]),
    help="Test framework to generate code for.",
)
def testgen(source_dir: str, output: str | None, framework: str) -> None:
    """Generate a Playwright integration test from a recorded session.

    SOURCE_DIR is the output directory produced by `sa stop`
    (must contain data.json).

    The generated file mirrors the recorded interaction chain:
      - Clicks become page.mouse.click() or page.get_by_text().click() calls.
      - Text input steps become page.keyboard.type() calls.
      - Screenshots become expect(page).to_have_screenshot() assertions.
      - Each step includes an inline comment with the original timestamp.

    Run the generated file with:
      pip install pytest-playwright
      playwright install
      pytest <output_file> --headed

    Examples:
      sa testgen psr_output\\psr_20240115_143000_output
      sa testgen psr_output\\psr_20240115_143000_output --output tests/integration/test_session.py
    """
    from psr_ai.core.testgen import generate_playwright_test

    out_path = Path(output) if output else None
    try:
        result = generate_playwright_test(
            session_dir=Path(source_dir),
            output_path=out_path,
            framework=framework,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    _echo(result.report())
    _echo(f"\nGenerated: {result.output_path}")
    if result.warnings:
        for w in result.warnings:
            click.echo(f"  Warning: {w}", err=True)


# ------------------------------------------------------------------
# sa pipeline
# ------------------------------------------------------------------

@main.command()
@click.argument("zip_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--branch", "-b",
    default=None,
    help=(
        "Git branch to create and push to. "
        "A new branch is created automatically when this is set."
    ),
)
@click.option(
    "--no-create-branch",
    "skip_create_branch",
    is_flag=True,
    default=False,
    help="Push to an existing branch instead of creating a new one.",
)
@click.option(
    "--analyze/--no-analyze",
    "run_analyze",
    default=False,
    help="Send screenshots to the vision LLM before publishing. Requires psr-ai[ai].",
)
@click.option(
    "--provider", "-p",
    default="anthropic",
    show_default=True,
    type=click.Choice(["anthropic", "openai"]),
    help="Vision LLM provider (only relevant when --analyze is set).",
)
@click.option(
    "--model", "-m",
    default=None,
    help="LLM model override for the analyze step.",
)
@click.option(
    "--pr",
    "open_pr",
    is_flag=True,
    default=False,
    help=(
        "Open a Pull Request on GitHub after pushing. "
        "Requires GITHUB_TOKEN and GITHUB_REPO environment variables."
    ),
)
@click.option(
    "--pr-base",
    default="main",
    show_default=True,
    help="Base branch for the Pull Request.",
)
@click.option(
    "--remote",
    default="origin",
    show_default=True,
    help="Git remote to push to.",
)
@click.option(
    "--message", "-M",
    default=None,
    help="Custom git commit message.",
)
@click.option(
    "--no-gif",
    "skip_gif",
    is_flag=True,
    default=False,
    help="Skip GIF generation.",
)
def pipeline(
    zip_path: str,
    branch: str | None,
    skip_create_branch: bool,
    run_analyze: bool,
    provider: str,
    model: str | None,
    open_pr: bool,
    pr_base: str,
    remote: str,
    message: str | None,
    skip_gif: bool,
) -> None:
    """Run the full stop-analyze-publish pipeline from a ZIP file.

    ZIP_PATH is the raw recorder output (produced by `sa stop` or the
    non-Windows recorders) - the .zip file itself, not a directory.

    Steps executed:
      1. Parse: extract the ZIP, render report.md and action_replay.gif.
      2. Analyze: (optional) send screenshots to a vision LLM.
      3. Publish: copy assets to docs/sessions/, commit, and push.
      4. PR: (optional) open a Pull Request on GitHub.

    Examples:
      sa pipeline psr_output/psr_20240115_143000.zip
      sa pipeline recording.zip --branch docs/session-20240115 --analyze --pr
      sa pipeline recording.zip --branch my-branch --no-create-branch
    """
    from psr_ai.core.pipeline import PipelineConfig, run_pipeline

    config = PipelineConfig(
        zip_path=Path(zip_path),
        project_root=Path.cwd(),
        branch=branch,
        create_branch=not skip_create_branch,
        analyze=run_analyze,
        provider=provider,
        model=model,
        github_pr=open_pr,
        pr_base=pr_base,
        remote=remote,
        commit_msg=message,
        no_gif=skip_gif,
    )

    result = run_pipeline(config, echo_fn=_echo)
    _echo(result.report())
    raise SystemExit(0 if result.success else 1)
