"""CLI entrypoint: defines the `sa` command group and its subcommands."""

import click

from psr_ai import __version__
from psr_ai.core.recorder import Recorder
from psr_ai.core.parser import parse_session
from psr_ai.core.generator import generate_markdown, generate_gif
from psr_ai.core.publisher import publish
from psr_ai.utils.paths import default_output_dir


@click.group()
@click.version_option(__version__, prog_name="sa")
def main() -> None:
    """PSR.ai - turn Windows Steps Recorder output into AI-ready documentation."""


@main.command()
@click.option(
    "--output", "-o",
    default=None,
    type=click.Path(),
    help="Path for the PSR output ZIP. Defaults to a timestamped file in the output dir.",
)
@click.option(
    "--maxsc",
    default=25,
    show_default=True,
    help="Maximum number of screenshots PSR will capture.",
)
def start(output: str | None, maxsc: int) -> None:
    """Start a new PSR recording session."""
    recorder = Recorder()
    out_path = recorder.start(output_path=output, max_screenshots=maxsc)
    click.echo(f"Recording started. Output will be saved to: {out_path}")


@main.command()
def stop() -> None:
    """Stop the active PSR recording session."""
    recorder = Recorder()
    zip_path = recorder.stop()
    if zip_path and zip_path.exists():
        click.echo(f"Recording stopped. File saved: {zip_path}")
    else:
        click.echo("Recording stopped (output path unknown or PSR exited on its own).")


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
    help="Also generate an animated GIF (requires the `gif` extra: pip install psr-ai[gif]).",
)
@click.option(
    "--publish", "do_publish",
    is_flag=True,
    default=False,
    help="Push generated docs to the configured GitHub repository.",
)
def pub(zip_file: str, out_dir: str | None, gif: bool, do_publish: bool) -> None:
    """Parse a PSR ZIP and publish Markdown documentation.

    ZIP_FILE is the path to the .zip produced by `sa stop` or PSR directly.
    """
    from pathlib import Path

    zip_path = Path(zip_file)
    output_dir = Path(out_dir) if out_dir else default_output_dir(zip_path)

    click.echo(f"Parsing: {zip_path.name}")
    session = parse_session(zip_path)
    click.echo(f"  Steps found: {len(session.steps)}")

    md_path = generate_markdown(session, output_dir)
    click.echo(f"  Markdown written: {md_path}")

    if gif:
        try:
            gif_path = generate_gif(session, output_dir)
            click.echo(f"  GIF written: {gif_path}")
        except ImportError:
            click.echo(
                "  GIF skipped: Pillow not installed. Run: pip install psr-ai[gif]",
                err=True,
            )

    if do_publish:
        result = publish(output_dir)
        click.echo(f"  Published: {result}")
    else:
        click.echo(f"Done. Output directory: {output_dir}")
