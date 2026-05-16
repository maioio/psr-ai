"""CLI entrypoint: defines the `sa` command group and its subcommands."""

from __future__ import annotations

from pathlib import Path

import click

from psr_ai import __version__
from psr_ai.core.recorder import PSRNotFoundError, Recorder
from psr_ai.core.parser import parse_session, generate_json_meta
from psr_ai.core.generator import GifGenerator, MarkdownGenerator, generate_markdown, generate_gif
from psr_ai.core.publisher import publish
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
    click.echo(f"Recording started.")
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
@click.argument("zip_file", type=click.Path(exists=True))
@click.option(
    "--out-dir", "-d",
    default=None,
    type=click.Path(),
    help="Directory for generated output. Defaults to a subfolder next to the ZIP.",
)
@click.option(
    "--gif",
    is_flag=True,
    default=False,
    help="Also generate an animated GIF (requires: pip install psr-ai[gif]).",
)
@click.option(
    "--publish", "do_publish",
    is_flag=True,
    default=False,
    help="Push the generated session.md to the configured GitHub repository.",
)
def pub(zip_file: str, out_dir: str | None, gif: bool, do_publish: bool) -> None:
    """Parse a PSR ZIP and publish Markdown documentation.

    ZIP_FILE is the path to the .zip produced by `sa stop` or PSR directly.
    """
    zip_path = Path(zip_file)
    output_dir = Path(out_dir) if out_dir else default_output_dir(zip_path)

    click.echo(f"Parsing: {zip_path.name}")
    session = parse_session(zip_path)
    click.echo(f"  Steps found: {len(session.steps)}")

    md_path = generate_markdown(session, output_dir)
    click.echo(f"  Markdown: {md_path}")

    if gif:
        try:
            gif_path = generate_gif(session, output_dir)
            click.echo(f"  GIF: {gif_path}")
        except ImportError:
            click.echo(
                "  GIF skipped: Pillow not installed. Run: pip install psr-ai[gif]",
                err=True,
            )

    if do_publish:
        result = publish(output_dir)
        click.echo(f"  Published: {result}")
    else:
        click.echo(f"Done. Output: {output_dir}")
