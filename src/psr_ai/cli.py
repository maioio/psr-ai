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
    default=100,
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
# sa capture / sa צלם
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output file path. Extension determines format: .pdf or .png (default).",
)
@click.option(
    "--pdf",
    "as_pdf",
    is_flag=True,
    default=False,
    help="Save as PDF instead of PNG screenshot.",
)
@click.option(
    "--crawl",
    "do_crawl",
    is_flag=True,
    default=False,
    help=(
        "Capture multiple pages of the site into one merged PDF. "
        "GitHub repos get a curated list; other sites follow nav links. "
        "Automatically enables --pdf. Requires: pip install pypdf"
    ),
)
@click.option(
    "--intent", "-i",
    default=None,
    help=(
        "What you care about on this site (e.g. 'anime episodes genres'). "
        "Used to prioritise which pages to capture when --crawl is active."
    ),
)
@click.option(
    "--max-pages",
    default=15,
    show_default=True,
    help="Maximum number of pages to capture when using --crawl (non-GitHub sites).",
)
def capture(
    url: str,
    output: str | None,
    as_pdf: bool,
    do_crawl: bool,
    intent: str | None,
    max_pages: int,
) -> None:
    """Capture a full-page screenshot or PDF from any URL.

    With --crawl, follows internal links and merges everything into one PDF.
    GitHub repos automatically get a curated page list (README, commits, issues…).
    For other sites, use --intent to describe what you want so the right pages
    are prioritised (e.g. --intent "episodes genres search").

    If you don't pass --intent, you'll be asked interactively.

    Examples:
      sa capture https://github.com/maio/myrepo --crawl
      sa capture https://myanimelist.net --crawl --intent "top anime genres"
      sa capture https://example.com --crawl --max-pages 20
      sa capture https://example.com --pdf
    """
    from psr_ai.core.capture import capture_page, crawl_site

    if do_crawl:
        as_pdf = True

        # Ask interactively if no intent was given and it's not a GitHub repo.
        import re as _re
        is_github = bool(_re.match(r"https?://github\.com/[^/]+/[^/]+", url))
        if not intent and not is_github:
            click.echo(f"Site: {url}")
            intent = click.prompt(
                "  What do you want from this site? "
                "(describe the pages/content that matter, or press Enter to capture everything)",
                default="",
            ).strip() or None

    if output:
        out_path = Path(output)
        if not out_path.suffix:
            out_path = out_path.with_suffix(".pdf" if as_pdf else ".png")
    else:
        stem = url.split("//")[-1].replace("/", "_").strip("_")[:60]
        suffix = ".pdf" if as_pdf else ".png"
        out_path = default_output_dir() / f"capture_{stem}{suffix}"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if do_crawl:
        click.echo(f"Crawling: {url}")
        if intent:
            click.echo(f"  Intent: {intent}")
        click.echo("  This may take a minute...")
        try:
            result = crawl_site(
                url, out_path,
                intent=intent,
                max_pages=max_pages,
                echo_fn=_echo,
            )
            _echo(f"  Saved: {result}")
        except ImportError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)
    else:
        fmt = "PDF" if as_pdf else "screenshot"
        click.echo(f"Capturing {fmt}: {url}")
        click.echo("  Please wait...")
        try:
            result = capture_page(url, out_path, as_pdf=as_pdf)
            _echo(f"  Saved: {result}")
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)


# Hebrew alias: sa צלם — wrapped to avoid UnicodeEncodeError on cp1252 terminals
try:
    main.add_command(capture, name="צלם")
except Exception:
    pass


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


# ------------------------------------------------------------------
# sa replay
# ------------------------------------------------------------------

@main.command()
@click.argument("zip_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Show what Claude would do without actually executing actions.",
)
@click.option(
    "--ask/--no-ask",
    "ask_before_action",
    default=False,
    show_default=True,
    help="Prompt for confirmation before each action. Default OFF (fully automatic).",
)
@click.option(
    "--max-steps",
    default=60,
    show_default=True,
    help="Hard limit on agent steps before stopping.",
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
    help="LLM model override.",
)
@click.option(
    "--speed",
    default=1.0,
    show_default=True,
    help="Speed multiplier on screenshot interval. 2.0 = slower (more stable); 0.5 = faster.",
)
@click.option(
    "--output-dir", "-o",
    default=None,
    type=click.Path(file_okay=False),
    help="Directory to save the replay session log.",
)
def replay(
    zip_path,
    dry_run,
    ask_before_action,
    max_steps,
    provider,
    model,
    speed,
    output_dir,
):
    """Replay a recorded session using Claude as the execution engine.

    Instead of replaying at exact coordinates (brittle macro), Claude sees
    the live screen at each step and adapts to the current UI state.

    ZIP_PATH is the path to a parsed session ZIP (produced by `sa stop` or
    `sa pipeline`). The session must contain a data.json file.

    By default runs in --dry-run mode (safe). Add --no-dry-run to execute.

    Examples:

      sa replay session.zip

      sa replay session.zip --no-dry-run --no-ask

      sa replay session.zip --provider openai --speed 0.5
    """
    from psr_ai.core.replay import ReplayConfig, run_replay

    if not dry_run and not ask_before_action:
        click.echo(
            "WARNING: running in fully autonomous mode (--no-dry-run --no-ask). "
            "Claude will execute all actions without confirmation.",
            err=True,
        )

    config = ReplayConfig(
        zip_path=Path(zip_path),
        dry_run=dry_run,
        ask_before_action=ask_before_action,
        max_steps=max_steps,
        provider=provider,
        model=model or None,
        speed=speed,
        output_dir=Path(output_dir) if output_dir else None,
    )

    result = run_replay(config, echo_fn=_echo)
    _echo(result.report())
    raise SystemExit(0 if result.success else 1)


# ------------------------------------------------------------------
# sa mcp
# ------------------------------------------------------------------

@main.command()
def mcp() -> None:
    """Start the PSR.ai MCP JSON-RPC server on stdin/stdout.

    Blocks the terminal and keeps stdin/stdout reserved for full-duplex
    communication with an MCP host (e.g. Claude Desktop, Claude Code).

    The server speaks JSON-RPC 2.0 over newline-delimited messages.
    Send Ctrl-C or close stdin to stop it.

    Tools exposed:
      psr_start_recording  - start a recording session
      psr_stop_and_parse   - stop, parse, and return the session report
      psr_quick_snapshot   - capture and return an optimized Base64 screenshot

    Example (manual test):
      echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | sa mcp
    """
    from psr_ai.mcp import main_loop
    main_loop()


# ------------------------------------------------------------------
# sa watch
# ------------------------------------------------------------------

@main.command()
@click.argument("trigger")
@click.option(
    "--action", "-a",
    required=True,
    help="What to do when the trigger is detected (natural language).",
)
@click.option(
    "--interval",
    default=5.0,
    show_default=True,
    help="Seconds between screen checks.",
)
@click.option(
    "--timeout",
    default=3600.0,
    show_default=True,
    help="Stop watching after this many seconds.",
)
@click.option(
    "--max-triggers",
    default=1,
    show_default=True,
    help="Stop after detecting and handling this many triggers.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Detect triggers but do not execute the action.",
)
@click.option(
    "--ask/--no-ask",
    "ask_before_action",
    default=False,
    show_default=True,
    help="Prompt before each action step. Default OFF.",
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
    help="LLM model override.",
)
@click.option(
    "--output-dir", "-o",
    default=None,
    type=click.Path(file_okay=False),
    help="Directory to save action session logs.",
)
def watch(
    trigger,
    action,
    interval,
    timeout,
    max_triggers,
    dry_run,
    ask_before_action,
    provider,
    model,
    output_dir,
):
    """Watch the screen and react automatically when a condition is detected.

    TRIGGER is a natural-language description of what to watch for.
    Claude checks the screen every INTERVAL seconds and asks itself
    whether the trigger condition is currently visible.

    When detected, Claude runs the --action task using the agent loop.

    By default runs in --dry-run mode: trigger detection is live but
    the action is not executed. Add --no-dry-run to act automatically.

    Examples:

      sa watch "a red error dialog" --action "close it and note the message"

      sa watch "the build finished" --action "run the test suite" --no-dry-run

      sa watch "download complete" --action "move file to Documents" --no-dry-run --interval 3

      sa watch "my name appears on screen" --action "take a screenshot" --max-triggers 5
    """
    from psr_ai.core.watcher import WatchConfig, run_watcher

    if not dry_run and not ask_before_action:
        click.echo(
            "WARNING: running in fully autonomous mode (--no-dry-run --no-ask). "
            "Claude will execute actions without confirmation when trigger fires.",
            err=True,
        )

    config = WatchConfig(
        trigger=trigger,
        action=action,
        interval=interval,
        timeout=timeout,
        max_triggers=max_triggers,
        dry_run=dry_run,
        ask_before_action=ask_before_action,
        provider=provider,
        model=model or None,
        output_dir=Path(output_dir) if output_dir else None,
    )

    result = run_watcher(config, echo_fn=_echo)
    _echo(result.report())
    raise SystemExit(0 if result.success else 1)


# ------------------------------------------------------------------
# sa browse
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory. Defaults to a timestamped directory in psr_output/.",
)
@click.option(
    "--wait-for",
    default=None,
    help="CSS selector to wait for before capturing (e.g. '.content-loaded').",
)
@click.option(
    "--delay",
    default=1.5,
    show_default=True,
    help="Seconds to wait for JS to settle after page load.",
)
def browse(url: str, output: str | None, wait_for: str | None, delay: float) -> None:
    """Capture a full-page screenshot and raw HTML from a URL.

    Unlike `sa capture`, this also saves the raw HTML source alongside the image,
    making it useful for downstream scraping or archiving.

    Examples:
      sa browse https://example.com
      sa browse https://example.com --wait-for ".article-body"
    """
    from psr_ai.core.scraper import browse_page

    out_dir = Path(output) if output else default_output_dir() / "browse"
    try:
        result = browse_page(url, out_dir, wait_for=wait_for, delay=delay)
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa scrape
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.argument("selector")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory. Defaults to a timestamped directory in psr_output/.",
)
@click.option(
    "--wait-for",
    default=None,
    help="CSS selector to wait for before extracting (e.g. '.results-loaded').",
)
@click.option(
    "--no-screenshot",
    "skip_screenshot",
    is_flag=True,
    default=False,
    help="Skip taking a screenshot alongside the scraped data.",
)
def scrape(
    url: str,
    selector: str,
    output: str | None,
    wait_for: str | None,
    skip_screenshot: bool,
) -> None:
    """Extract elements matching a CSS selector and save as JSON.

    SELECTOR is any valid CSS selector (e.g. 'article.post', 'table tr', '.product-card').

    Examples:
      sa scrape https://news.ycombinator.com "tr.athing"
      sa scrape https://example.com ".product" --wait-for ".loaded"
    """
    from psr_ai.core.scraper import scrape_page

    out_dir = Path(output) if output else default_output_dir() / "scrape"
    try:
        result = scrape_page(
            url, selector, out_dir,
            wait_for=wait_for,
            screenshot=not skip_screenshot,
        )
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa form
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory.",
)
@click.option(
    "--wait-for",
    default=None,
    help="CSS selector to wait for before capturing.",
)
def form(url: str, output: str | None, wait_for: str | None) -> None:
    """Browse to a URL and capture a full-page screenshot (alias for sa browse).

    Useful for capturing form pages before filling them in.

    Examples:
      sa form https://example.com/contact
    """
    from psr_ai.core.scraper import browse_page

    out_dir = Path(output) if output else default_output_dir() / "form"
    try:
        result = browse_page(url, out_dir, wait_for=wait_for)
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa codegen
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output .py test file path. Defaults to codegen_test.py in psr_output/.",
)
@click.option(
    "--browser", "-b",
    default="chromium",
    show_default=True,
    type=click.Choice(["chromium", "firefox", "webkit"]),
    help="Browser engine to use for recording.",
)
@click.option(
    "--data-dir",
    default=None,
    type=click.Path(),
    help="Directory to save the codegen_data.json step log.",
)
def codegen(
    url: str,
    output: str | None,
    browser: str,
    data_dir: str | None,
) -> None:
    """Record browser interactions and generate a Playwright test file.

    Opens a browser window at URL. Interact with the page normally.
    When you close the browser, all interactions are saved as a runnable pytest file.

    Requires: pip install psr-ai[capture]

    Examples:
      sa codegen https://example.com
      sa codegen https://myapp.com/login --output tests/test_login.py
      sa codegen https://example.com --browser firefox
    """
    from psr_ai.core.codegen import record_session

    if output:
        out_file = Path(output)
    else:
        out_file = default_output_dir() / "codegen_test.py"

    out_dir = Path(data_dir) if data_dir else out_file.parent

    try:
        result = record_session(url, out_file, browser=browser, output_dir=out_dir, echo_fn=_echo)
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa intercept
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the network log and screenshot.",
)
@click.option(
    "--block",
    "block_domains",
    multiple=True,
    help="Domain to block (can be repeated). E.g. --block ads.example.com",
)
@click.option(
    "--no-screenshot",
    "skip_screenshot",
    is_flag=True,
    default=False,
    help="Skip taking a screenshot.",
)
def intercept(
    url: str,
    output: str | None,
    block_domains: tuple,
    skip_screenshot: bool,
) -> None:
    """Load a page and log all network requests to JSON.

    Optionally block specific domains (useful for ad/tracker removal).
    The full request log is saved as <stem>_network.json.

    Examples:
      sa intercept https://example.com
      sa intercept https://example.com --block ads.example.com --block tracker.io
    """
    from psr_ai.core.scraper import intercept_network

    out_dir = Path(output) if output else default_output_dir() / "intercept"
    try:
        result = intercept_network(
            url, out_dir,
            block_domains=list(block_domains),
            screenshot=not skip_screenshot,
        )
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa test-all
# ------------------------------------------------------------------

@main.command("test-all")
@click.argument("test_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for per-browser HTML reports.",
)
@click.option(
    "--browsers",
    default="chromium,firefox,webkit",
    show_default=True,
    help="Comma-separated list of browsers to test on.",
)
def test_all(test_file: str, output: str | None, browsers: str) -> None:
    """Run a Playwright test file across multiple browser engines in sequence.

    Generates one HTML report per browser engine in the output directory.
    Requires pytest-playwright and pytest-html:
      pip install psr-ai[capture] pytest-playwright pytest-html

    Examples:
      sa test-all tests/test_ui.py
      sa test-all tests/test_ui.py --browsers chromium,firefox
    """
    from psr_ai.core.scraper import run_parallel_tests

    browser_list = [b.strip() for b in browsers.split(",") if b.strip()]
    out_dir = Path(output) if output else default_output_dir() / "test_all"
    try:
        result = run_parallel_tests(Path(test_file), browser_list, out_dir, echo_fn=_echo)
        _echo(result.report())
        raise SystemExit(0 if result.all_passed else 1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa accessibility
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the accessibility report.",
)
def accessibility(url: str, output: str | None) -> None:
    """Run a lightweight accessibility audit against a URL.

    Checks for: missing image alt text, unlabeled inputs, empty buttons,
    empty links, missing document title, and skipped heading levels.

    Saves both a Markdown report and a JSON violation log.

    Requires: pip install psr-ai[capture]

    Examples:
      sa accessibility https://example.com
    """
    from psr_ai.core.scraper import run_accessibility_audit

    out_dir = Path(output) if output else default_output_dir() / "accessibility"
    try:
        result = run_accessibility_audit(url, out_dir, echo_fn=_echo)
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa trace
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the trace ZIP and screenshot.",
)
@click.option(
    "--no-screenshot",
    "skip_screenshot",
    is_flag=True,
    default=False,
    help="Skip the accompanying screenshot.",
)
def trace(url: str, output: str | None, skip_screenshot: bool) -> None:
    """Capture a Playwright trace (network + DOM snapshots) while loading a URL.

    The trace ZIP can be inspected with `sa trace-show <trace.zip>` or via
    https://trace.playwright.dev

    Requires: pip install psr-ai[capture]

    Examples:
      sa trace https://example.com
      sa trace-show psr_output/trace/example_com_trace.zip
    """
    from psr_ai.core.scraper import capture_trace

    out_dir = Path(output) if output else default_output_dir() / "trace"
    try:
        result = capture_trace(url, out_dir, screenshot=not skip_screenshot, echo_fn=_echo)
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa trace-show
# ------------------------------------------------------------------

@main.command("trace-show")
@click.argument("trace_path", type=click.Path(exists=True, dir_okay=False))
def trace_show(trace_path: str) -> None:
    """Open a Playwright trace ZIP in the trace viewer.

    Requires: pip install psr-ai[capture]

    Examples:
      sa trace-show psr_output/trace/example_com_trace.zip
    """
    from psr_ai.core.scraper import show_trace

    try:
        show_trace(Path(trace_path))
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa scrape-species
# ------------------------------------------------------------------

@main.command("scrape-species")
@click.argument("query")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the species JSON data.",
)
@click.option(
    "--sources",
    default="inat,gbif,bugguide",
    show_default=True,
    help="Comma-separated list of sources to query.",
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    help="Maximum results per source.",
)
def scrape_species(query: str, output: str | None, sources: str, limit: int) -> None:
    """Look up a species across iNaturalist, GBIF, and BugGuide.

    QUERY can be a scientific name (e.g. 'Phasma gigas') or a common name
    (e.g. 'stick insect'). Results are printed and optionally saved as JSON.

    Examples:
      sa scrape-species "stick insect"
      sa scrape-species "Phasma gigas" --sources inat,gbif
      sa scrape-species "Mantis religiosa" --output psr_output/species
    """
    from psr_ai.core.species import lookup_species

    out_dir = Path(output) if output else None
    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    try:
        result = lookup_species(query, output_dir=out_dir, sources=source_list, limit=limit)
        _echo(result.report())
        if result.error:
            raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa download
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory. Defaults to a timestamped directory in psr_output/.",
)
@click.option(
    "--audio",
    "audio_only",
    is_flag=True,
    default=False,
    help="Extract audio only (saved as MP3).",
)
@click.option(
    "--quality", "-q",
    default=None,
    help="Max video resolution (e.g. 720, 1080). Omit for best available.",
)
@click.option(
    "--subtitles",
    is_flag=True,
    default=False,
    help="Download subtitles alongside the video.",
)
def download(
    url: str,
    output: str | None,
    audio_only: bool,
    quality: str | None,
    subtitles: bool,
) -> None:
    """Download a video or audio file from any supported URL.

    Uses yt-dlp under the hood; falls back to gallery-dl for image galleries.
    Supports YouTube, Vimeo, Twitter/X, Reddit, TikTok, Instagram, and more.

    Requires: pip install psr-ai[download]

    Examples:
      sa download https://www.youtube.com/watch?v=dQw4w9WgXcQ
      sa download https://www.youtube.com/watch?v=dQw4w9WgXcQ --audio
      sa download https://www.youtube.com/watch?v=dQw4w9WgXcQ --quality 720
    """
    from psr_ai.core.downloader import download_media

    out_dir = Path(output) if output else default_output_dir() / "downloads"
    try:
        result = download_media(
            url, out_dir,
            audio_only=audio_only,
            quality=quality,
            subtitles=subtitles,
            echo_fn=_echo,
        )
        _echo(result.report())
        raise SystemExit(0 if result.success else 1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa download-profile
# ------------------------------------------------------------------

@main.command("download-profile")
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory.",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Maximum number of items to download.",
)
def download_profile(url: str, output: str | None, limit: int | None) -> None:
    """Download all media from a user profile or channel.

    Uses gallery-dl if available, then yt-dlp as fallback.
    Useful for archiving Instagram, Twitter/X, Flickr, DeviantArt, and similar profiles.

    Requires: pip install psr-ai[download]

    Examples:
      sa download-profile https://www.instagram.com/example_user/
      sa download-profile https://twitter.com/example_user --limit 50
    """
    from psr_ai.core.downloader import download_profile as _download_profile

    out_dir = Path(output) if output else default_output_dir() / "downloads"
    try:
        result = _download_profile(url, out_dir, limit=limit, echo_fn=_echo)
        _echo(result.report())
        raise SystemExit(0 if result.success else 1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa instagram
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory. Defaults to psr_output/instagram/.",
)
@click.option(
    "--session", "-s",
    default=None,
    type=click.Path(),
    help="Path to session JSON file. Defaults to ~/.psr_instagram_session.json.",
)
@click.option(
    "--username", "-u",
    default=None,
    envvar="IG_USERNAME",
    help="Instagram username for first-time login (or set IG_USERNAME env var).",
)
@click.option(
    "--password", "-p",
    default=None,
    envvar="IG_PASSWORD",
    help="Instagram password for first-time login (or set IG_PASSWORD env var).",
)
def instagram(
    url: str,
    output: str | None,
    session: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Download Instagram Reels, posts, or albums - fully headless.

    Uses Instagram's private mobile API (instagrapi) - no browser window opens.
    First run requires --username and --password to authenticate; subsequent
    runs reuse the saved session file automatically.

    Requires: pip install psr-ai[instagram]

    Examples:
      sa instagram https://www.instagram.com/reel/ABC123/
      sa instagram https://www.instagram.com/p/XYZ789/ --username me --password secret
      sa instagram https://www.instagram.com/reel/ABC123/ --session ./my_session.json
      sa instagram https://www.instagram.com/reel/ABC123/ --output ./downloads/ig/
    """
    from psr_ai.core.downloader import download_instagram

    out_dir = Path(output) if output else default_output_dir() / "instagram"
    sess_path = Path(session) if session else None

    try:
        result = download_instagram(
            url,
            out_dir,
            session_file=sess_path,
            username=username,
            password=password,
            echo_fn=_echo,
        )
        _echo(result.report())
        raise SystemExit(0 if result.success else 1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa video-analyze
# ------------------------------------------------------------------

@main.command("video-analyze")
@click.argument("source")
@click.option(
    "--question", "-q",
    default=None,
    help="What to ask Claude about the video. Default: full summary with TL;DR, key points, quotes.",
)
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory. Defaults to psr_output/video_analysis/.",
)
@click.option(
    "--fps",
    default=0.5,
    type=float,
    show_default=True,
    help="Frames per second to sample (0.5 = 1 frame every 2 seconds).",
)
@click.option(
    "--max-frames",
    default=300,
    type=int,
    show_default=True,
    help="Maximum number of frames to extract.",
)
@click.option(
    "--whisper-model",
    default="base",
    show_default=True,
    type=click.Choice(["tiny", "base", "small", "medium", "large"]),
    help="Whisper model size for transcription.",
)
def video_analyze(
    source: str,
    question: str | None,
    output: str | None,
    fps: float,
    max_frames: int,
    whisper_model: str,
) -> None:
    """Analyze a video using frames + audio transcription + Claude.

    SOURCE can be a URL (YouTube, TikTok, Instagram Reel, Twitter/X, etc.)
    or a local video file path.

    Claude receives sampled frames and the full audio transcript, then
    answers your question or produces a structured summary.

    Requires: pip install psr-ai[analyze]

    Examples:
      sa video-analyze https://www.youtube.com/watch?v=abc123
      sa video-analyze https://www.tiktok.com/@user/video/123 -q "what is this about?"
      sa video-analyze lecture.mp4 -q "summarize the key points"
      sa video-analyze video.mp4 --whisper-model small --fps 1
    """
    from psr_ai.core.video_analyzer import analyze_video

    out_dir = Path(output) if output else default_output_dir() / "video_analysis"
    default_question = (
        "Please analyze this video. Provide:\n"
        "1. TL;DR (2-3 sentences)\n"
        "2. Key points (bullet list)\n"
        "3. Notable quotes or moments\n"
        "4. Main takeaways"
    )

    try:
        result = analyze_video(
            source,
            out_dir,
            question=question or default_question,
            fps=fps,
            max_frames=max_frames,
            whisper_model=whisper_model,
            echo_fn=_echo,
        )
        _echo(result.report())
        raise SystemExit(0 if result.success else 1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa api
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--method", "-X",
    default="GET",
    show_default=True,
    type=click.Choice(["GET", "POST", "PUT", "DELETE", "PATCH"], case_sensitive=False),
    help="HTTP method.",
)
@click.option(
    "--data", "-d",
    default=None,
    help="JSON request body (for POST/PUT/PATCH). Pass as a JSON string.",
)
@click.option(
    "--header", "-H",
    "headers",
    multiple=True,
    help="Request header in 'Name: Value' format. Repeatable.",
)
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the response JSON.",
)
@click.option(
    "--assert-status",
    default=None,
    type=int,
    help="Assert that the response status matches this code. Exits 1 if not.",
)
@click.option(
    "--storage-state",
    default=None,
    type=click.Path(exists=True),
    help="Path to browser storage_state.json to reuse auth cookies.",
)
def api(
    url: str,
    method: str,
    data: str | None,
    headers: tuple,
    output: str | None,
    assert_status: int | None,
    storage_state: str | None,
) -> None:
    """Send an HTTP request using Playwright's API testing layer (no browser).

    Supports reusing a saved browser authentication session via --storage-state,
    making it ideal for testing APIs that require browser-based login first.

    Requires: pip install psr-ai[capture]

    Examples:
      sa api https://api.example.com/users
      sa api https://api.example.com/items --method POST --data '{"name":"test"}'
      sa api https://api.example.com/me --storage-state auth.json
      sa api https://httpbin.org/status/200 --assert-status 200
    """
    import json as _json
    from psr_ai.core.scraper import api_request

    parsed_headers: dict = {}
    for h in headers:
        if ":" in h:
            k, v = h.split(":", 1)
            parsed_headers[k.strip()] = v.strip()

    parsed_data: dict | None = None
    if data:
        try:
            parsed_data = _json.loads(data)
        except _json.JSONDecodeError:
            click.echo(f"Error: --data must be valid JSON. Got: {data!r}", err=True)
            raise SystemExit(1)

    out_dir = Path(output) if output else None
    try:
        result = api_request(
            url,
            method=method.upper(),
            data=parsed_data,
            headers=parsed_headers or None,
            output_dir=out_dir,
            storage_state=storage_state,
            assert_status=assert_status,
            echo_fn=_echo,
        )
        _echo(result.report())
        raise SystemExit(0 if result.ok else 1)
    except AssertionError as exc:
        click.echo(f"Assertion failed: {exc}", err=True)
        raise SystemExit(1)
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa video
# ------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Output directory for the video (.webm) and screenshot.",
)
@click.option(
    "--browser", "-b",
    default="chromium",
    show_default=True,
    type=click.Choice(["chromium", "firefox", "webkit"]),
    help="Browser engine to use.",
)
@click.option(
    "--headed",
    "headed",
    is_flag=True,
    default=False,
    help="Show the browser window while recording.",
)
def video(url: str, output: str | None, browser: str, headed: bool) -> None:
    """Record a video (.webm) of loading a URL.

    Requires: pip install psr-ai[capture]

    Examples:
      sa video https://example.com
      sa video https://example.com --browser firefox --headed
    """
    from psr_ai.core.scraper import record_video

    out_dir = Path(output) if output else default_output_dir() / "video"
    try:
        result = record_video(
            url, out_dir,
            browser_name=browser,
            headless=not headed,
            echo_fn=_echo,
        )
        _echo(result.report())
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa do  (natural language interface)
# ------------------------------------------------------------------

@main.command("do")
@click.argument("instruction")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what command would be run without executing it.",
)
@click.option(
    "--lang",
    default=None,
    help="Override language detection (e.g. en, he, ar, ru, es, fr, de).",
)
def do_command(instruction: str, dry_run: bool, lang: str | None) -> None:
    """Run any PSR.ai capability using plain language (any language).

    PSR.ai will figure out what you want and execute the right command.
    Works with English, Hebrew, Arabic, Russian, Spanish, French, German,
    Italian, Portuguese, Turkish, Chinese, Japanese, Korean, and more.

    Examples:
      sa do "take a screenshot of google.com"
      sa do "תוריד לי את הסרטון הזה https://youtube.com/..."
      sa do "check accessibility of https://example.com"
      sa do "تحميل هذا الفيديو https://..."
      sa do "descargar el video de https://..."
      sa do "截图 https://example.com"
      sa do "Prueba de accesibilidad en https://example.com"
    """
    from psr_ai.core.intent import classify, intent_to_cli_args

    intent = classify(instruction)

    if lang:
        intent.language = lang

    _echo(f"Detected: action={intent.action!r}  confidence={intent.confidence:.0%}  lang={intent.language}")

    if intent.clarification:
        click.echo(f"\n{intent.clarification}")
        raise SystemExit(0)

    cli_args = intent_to_cli_args(intent)

    if dry_run:
        click.echo(f"Would run: sa {' '.join(cli_args)}")
        raise SystemExit(0)

    _echo(f"Running: sa {' '.join(cli_args)}")
    _echo("")

    # Invoke the resolved subcommand via Click's standalone mode
    try:
        from click.testing import CliRunner
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(main, cli_args, catch_exceptions=False)
        if result.output:
            click.echo(result.output, nl=False)
        raise SystemExit(result.exit_code)
    except SystemExit:
        raise
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa locate  -- Locator API
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--css",         default=None, help="CSS selector")
@click.option("--xpath",       default=None, help="XPath expression")
@click.option("--role",        default=None, help="ARIA role (e.g. button, link, heading)")
@click.option("--name",        default=None, help="Accessible name (used with --role)")
@click.option("--text",        default=None, help="Visible text content")
@click.option("--label",       default=None, help="Form label text")
@click.option("--placeholder", default=None, help="Input placeholder text")
@click.option("--alt",         default=None, help="Image alt text")
@click.option("--title",       default=None, help="Element title attribute")
@click.option("--test-id",     default=None, help="data-testid attribute")
@click.option("--nth",         default=None, type=int, help="0-based index of matched element")
@click.option("--no-screenshot", "skip_shot", is_flag=True, default=False)
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def locate(url, css, xpath, role, name, text, label, placeholder, alt, title,
           test_id, nth, skip_shot, output, browser):
    """Locate elements using Playwright's modern Locator API (get_by_role, get_by_text, etc.).

    Examples:
      sa locate https://example.com --role heading
      sa locate https://example.com --text "More information"
      sa locate https://example.com --css "h1"
      sa locate https://example.com --role link --name "More information"
    """
    from psr_ai.core.scraper import locate_elements
    out_dir = Path(output) if output else default_output_dir() / "locate"
    try:
        result = locate_elements(
            url=url, output_dir=out_dir,
            strategy="xpath" if xpath else "css",
            value=xpath or css or "body",
            role=role, name=name, text=text, label=label,
            placeholder=placeholder, alt_text=alt, title=title,
            test_id=test_id, nth=nth,
            screenshot=not skip_shot, browser_name=browser,
        )
        _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa assert  -- Expect assertions
# ---------------------------------------------------------------------------

@main.command("assert")
@click.argument("url")
@click.option("--visible",      multiple=True, metavar="SELECTOR", help="Assert element is visible")
@click.option("--hidden",       multiple=True, metavar="SELECTOR", help="Assert element is hidden")
@click.option("--enabled",      multiple=True, metavar="SELECTOR", help="Assert element is enabled")
@click.option("--disabled",     multiple=True, metavar="SELECTOR", help="Assert element is disabled")
@click.option("--text",         multiple=True, nargs=2, metavar="SELECTOR VALUE", help="Assert element text")
@click.option("--count",        multiple=True, nargs=2, metavar="SELECTOR N", help="Assert element count")
@click.option("--url-pattern",  default=None, help="Assert page URL matches pattern")
@click.option("--title-pattern",default=None, help="Assert page title matches pattern")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser",      default="chromium", show_default=True)
def assert_cmd(url, visible, hidden, enabled, disabled, text, count,
               url_pattern, title_pattern, output, browser):
    """Run Playwright expect() assertions against a live URL.

    Examples:
      sa assert https://example.com --visible h1 --title-pattern "Example"
      sa assert https://example.com --text h1 "Example Domain" --count "p" 2
    """
    from psr_ai.core.scraper import run_assertions
    out_dir = Path(output) if output else default_output_dir() / "assertions"

    assertions: list[dict] = []
    if url_pattern:
        assertions.append({"type": "page_url", "pattern": url_pattern})
    if title_pattern:
        assertions.append({"type": "page_title", "pattern": title_pattern})
    for sel in visible:
        assertions.append({"type": "visible", "selector": sel})
    for sel in hidden:
        assertions.append({"type": "hidden", "selector": sel})
    for sel in enabled:
        assertions.append({"type": "enabled", "selector": sel})
    for sel in disabled:
        assertions.append({"type": "disabled", "selector": sel})
    for sel, val in text:
        assertions.append({"type": "text", "selector": sel, "value": val})
    for sel, n in count:
        assertions.append({"type": "count", "selector": sel, "value": int(n)})

    if not assertions:
        click.echo("No assertions specified. Use --visible, --text, --url-pattern, etc.", err=True)
        raise SystemExit(1)

    try:
        result = run_assertions(url, out_dir, assertions, browser_name=browser)
        _echo(result.report())
        if not result.all_passed:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa har  -- HAR record + playback
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--play",    default=None, type=click.Path(exists=True),
              help="HAR file to replay instead of hitting the network")
@click.option("--filter",  "url_filter", default=None, help="URL glob to include in HAR")
@click.option("--update-missing", is_flag=True, default=False,
              help="Fall back to network for requests not in HAR (playback only)")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def har(url, play, url_filter, update_missing, output, browser):
    """Record or replay a HAR (HTTP Archive) file.

    Record:  sa har https://example.com
    Replay:  sa har https://example.com --play session.har
    """
    from psr_ai.core.scraper import record_har, playback_har
    out_dir = Path(output) if output else default_output_dir() / "har"
    try:
        if play:
            result = playback_har(url, Path(play), out_dir,
                                  update_missing=update_missing, browser_name=browser)
            _echo(f"HAR replay: {url}")
            _echo(result.report())
        else:
            result = record_har(url, out_dir, url_filter=url_filter, browser_name=browser)
            _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa screenshot  -- Advanced screenshot
# ---------------------------------------------------------------------------

@main.command("screenshot")
@click.argument("url")
@click.option("--format", "fmt", default="png", type=click.Choice(["png", "jpeg"]), show_default=True)
@click.option("--quality", default=None, type=int, help="JPEG quality 0-100")
@click.option("--no-full-page", "no_full", is_flag=True, default=False)
@click.option("--selector",  default=None, help="CSS selector to screenshot a single element")
@click.option("--mask",      multiple=True, metavar="SELECTOR", help="Selectors to mask out (pink box)")
@click.option("--omit-background", is_flag=True, default=False, help="Transparent background (PNG only)")
@click.option("--animations", default="allow",
              type=click.Choice(["allow", "disabled"]), show_default=True)
@click.option("--scale", default="device", type=click.Choice(["device", "css"]), show_default=True)
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def screenshot_cmd(url, fmt, quality, no_full, selector, mask, omit_background,
                   animations, scale, output, browser):
    """Take an advanced screenshot with full Playwright option support.

    Examples:
      sa screenshot https://example.com --format jpeg --quality 85
      sa screenshot https://example.com --selector h1 --mask ".ads"
      sa screenshot https://example.com --omit-background
    """
    from psr_ai.core.scraper import screenshot_advanced
    out_dir = Path(output) if output else default_output_dir() / "screenshots"
    try:
        result = screenshot_advanced(
            url=url, output_dir=out_dir, fmt=fmt, quality=quality,
            full_page=not no_full, selector=selector,
            mask_selectors=list(mask) or None,
            omit_background=omit_background, animations=animations,
            scale=scale, browser_name=browser,
        )
        _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa pdf  -- Advanced PDF
# ---------------------------------------------------------------------------

@main.command("pdf")
@click.argument("url")
@click.option("--format", "fmt", default="A4", show_default=True,
              help="Page format: A4, Letter, A3, Legal, Tabloid, etc.")
@click.option("--landscape", is_flag=True, default=False)
@click.option("--no-background", "no_bg", is_flag=True, default=False)
@click.option("--margin-top",    default=None, help="e.g. 1cm, 0.5in")
@click.option("--margin-bottom", default=None)
@click.option("--margin-left",   default=None)
@click.option("--margin-right",  default=None)
@click.option("--header", default=None, help="HTML for page header")
@click.option("--footer", default=None, help="HTML for page footer")
@click.option("--page-ranges",   default=None, help="e.g. '1-3, 5'")
@click.option("--scale",  default=1.0, type=float, show_default=True)
@click.option("--output", "-o",  default=None, type=click.Path())
@click.option("--browser",       default="chromium", show_default=True)
def pdf_cmd(url, fmt, landscape, no_bg, margin_top, margin_bottom,
            margin_left, margin_right, header, footer, page_ranges, scale, output, browser):
    """Generate a PDF with full layout control.

    Examples:
      sa pdf https://example.com --landscape --format Letter
      sa pdf https://example.com --margin-top 2cm --footer "<span>Page <span class='pageNumber'></span></span>"
      sa pdf https://example.com --page-ranges "1-2"
    """
    from psr_ai.core.scraper import generate_pdf
    out_dir = Path(output) if output else default_output_dir() / "pdf"
    margin: dict | None = None
    if any([margin_top, margin_bottom, margin_left, margin_right]):
        margin = {
            "top":    margin_top    or "0",
            "bottom": margin_bottom or "0",
            "left":   margin_left   or "0",
            "right":  margin_right  or "0",
        }
    try:
        result = generate_pdf(
            url=url, output_dir=out_dir, fmt=fmt, landscape=landscape,
            print_background=not no_bg, margin=margin,
            header_template=header, footer_template=footer,
            page_ranges=page_ranges, scale=scale, browser_name=browser,
        )
        _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa clock  -- Clock API
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--fixed-time", default=None,
              help="Freeze clock at ISO-8601 time, e.g. 2024-01-15T10:00:00")
@click.option("--tick",  default=None, type=int, help="Advance clock by N ms after load")
@click.option("--run-for", default=None, type=int, help="Fire all timers for N ms")
@click.option("--eval", "eval_script", default=None, help="JS to evaluate after clock ops")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def clock(url, fixed_time, tick, run_for, eval_script, output, browser):
    """Browse a URL with the browser clock frozen or advanced.

    Examples:
      sa clock https://example.com --fixed-time "2024-12-31T23:59:59"
      sa clock https://example.com --tick 3600000
      sa clock https://example.com --run-for 5000 --eval "document.querySelector('.timer').innerText"
    """
    from psr_ai.core.scraper import browse_with_clock
    out_dir = Path(output) if output else default_output_dir() / "clock"
    try:
        result = browse_with_clock(
            url=url, output_dir=out_dir, fixed_time=fixed_time,
            tick_ms=tick, run_for_ms=run_for, eval_script=eval_script,
            browser_name=browser,
        )
        _echo(result.report())
        if result.evaluated is not None:
            _echo(f"  Eval result: {result.evaluated}")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa cookies  -- Cookie management
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--add",   multiple=True, metavar="name=value",
              help="Add cookie in name=value format (domain inferred from URL)")
@click.option("--clear", is_flag=True, default=False, help="Clear all cookies before loading")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def cookies(url, add, clear, output, browser):
    """Get, add, or clear cookies for a URL.

    Examples:
      sa cookies https://example.com
      sa cookies https://example.com --add session=abc123 --add lang=en
      sa cookies https://example.com --clear
    """
    from psr_ai.core.scraper import manage_cookies
    import urllib.parse
    out_dir = Path(output) if output else default_output_dir() / "cookies"
    domain = urllib.parse.urlparse(url).hostname or ""
    add_cookies: list[dict] | None = None
    if add:
        add_cookies = []
        for item in add:
            if "=" in item:
                k, v = item.split("=", 1)
                add_cookies.append({"name": k.strip(), "value": v.strip(), "domain": domain, "path": "/"})
    try:
        result = manage_cookies(url, out_dir, add_cookies=add_cookies,
                                clear=clear, browser_name=browser)
        _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa websockets  -- WebSocket monitor
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--wait", "wait_ms", default=5000, show_default=True,
              help="Ms to wait for WS traffic after page load")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def websockets(url, wait_ms, output, browser):
    """Monitor WebSocket frames on a page.

    Examples:
      sa websockets wss://echo.websocket.org
      sa websockets https://example.com --wait 10000
    """
    from psr_ai.core.scraper import monitor_websockets
    out_dir = Path(output) if output else default_output_dir() / "websockets"
    try:
        result = monitor_websockets(url, out_dir, wait_ms=wait_ms, browser_name=browser)
        _echo(result.report())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa events  -- page errors, request failures, service workers
# ---------------------------------------------------------------------------

@main.command()
@click.argument("url")
@click.option("--output", "-o", default=None, type=click.Path())
@click.option("--browser", default="chromium", show_default=True)
def events(url, output, browser):
    """Capture page errors, failed requests, service workers, and console messages.

    Examples:
      sa events https://example.com
    """
    from psr_ai.core.scraper import monitor_page_events
    out_dir = Path(output) if output else default_output_dir() / "events"
    try:
        result = monitor_page_events(url, out_dir, browser_name=browser)
        _echo(result.report())
        if result.output_file:
            _echo(f"  Saved: {result.output_file}")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# sa devices  -- list available device descriptors
# ---------------------------------------------------------------------------

@main.command()
@click.option("--filter", "pattern", default=None, help="Filter by name substring (e.g. iphone)")
def devices(pattern):
    """List all available device emulation descriptors.

    Examples:
      sa devices
      sa devices --filter iphone
      sa devices --filter pixel
    """
    from psr_ai.core.scraper import DEVICE_MAP
    names = sorted(DEVICE_MAP.keys())
    if pattern:
        names = [n for n in names if pattern.lower() in n]
    click.echo(f"Available devices ({len(names)}):")
    for name in names:
        dev = DEVICE_MAP[name]
        vp = dev.get("viewport", {})
        w, h = vp.get("width", "?"), vp.get("height", "?")
        mobile = "mobile" if dev.get("is_mobile") else "desktop"
        click.echo(f"  {name:<30} {w}x{h}  ({mobile})")
