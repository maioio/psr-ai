"""CLI entrypoint: defines the `sa` command group and its subcommands."""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

import click


def _load_dotenv() -> None:
    """Load ~/.env and ./.env into os.environ (no dependencies required)."""
    for env_path in (Path.home() / ".env", Path(".env")):
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("\"'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            pass


_load_dotenv()

from psr_ai import __version__
from psr_ai.core.recorder import PSRNotFoundError, Recorder
from psr_ai.core.parser import parse_session, generate_json_meta
from psr_ai.core.generator import GifGenerator, MarkdownGenerator, generate_markdown, generate_gif
from psr_ai.core.reviewer import run_review_session
from psr_ai.core.publisher import Publisher, PublishError, publish_via_github_api
from psr_ai.utils.paths import default_output_dir

# analyzer is imported lazily inside sa analyze to avoid requiring the ai extra
# for users who never use the command.


def _ensure_utf8_stdout() -> None:
    """Switch stdout/stderr to UTF-8 when the console can be reconfigured.

    Windows consoles default to a legacy code page (cp1252 here), which turns
    every non-ASCII character in a path into '?'. That is merely ugly for a log
    line, but fatal for output the user must copy verbatim - the extension
    directory printed by `sa context install`, for instance. Reconfiguring is
    the fix; the ASCII fallback in _echo stays as the last resort.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if getattr(stream, "encoding", "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass   # Not a real console (pytest capture, pipes) - nothing to do.


def _echo(msg: str) -> None:
    """click.echo with a safe fallback for terminals that cannot encode Unicode paths."""
    try:
        click.echo(msg)
    except UnicodeEncodeError:
        click.echo(msg.encode("ascii", errors="replace").decode("ascii"))


# ------------------------------------------------------------------
# Output discipline
# ------------------------------------------------------------------
#
# Quiet is the default and it is global: the setting comes from the flag or
# the PSR_AI_QUIET / PSR_AI_VERBOSE environment variables, never from a file
# in the current directory, so `sa` behaves identically in every directory and
# never has to be re-configured per project. In quiet mode the commands print
# only their final result - progress chatter, step-by-step narration, and
# third-party warnings are suppressed. `sa --verbose <cmd>` restores the full
# narration for debugging.

def _env_flag(name: str) -> bool | None:
    """Read a tri-state boolean env var: True, False, or None when unset."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _initial_verbosity() -> bool:
    explicit = _env_flag("PSR_AI_VERBOSE")
    if explicit is not None:
        return explicit
    quiet = _env_flag("PSR_AI_QUIET")
    if quiet is not None:
        return not quiet
    return False


_VERBOSE = _initial_verbosity()


def _set_verbosity(verbose: bool) -> None:
    """Apply the verbosity setting to this process and everything it spawns."""
    global _VERBOSE
    _VERBOSE = verbose
    os.environ["PSR_AI_VERBOSE"] = "1" if verbose else "0"
    os.environ["PSR_AI_QUIET"] = "0" if verbose else "1"
    if not verbose:
        # Library deprecation notices are background noise, not results.
        warnings.filterwarnings("ignore")
        os.environ.setdefault("PYTHONWARNINGS", "ignore")


def _detail(msg: str) -> None:
    """Print progress detail - suppressed unless --verbose is active."""
    if _VERBOSE:
        _echo(msg)


def _warn(msg: str) -> None:
    """Print a warning on stderr so stdout stays a clean result stream."""
    try:
        click.echo(msg, err=True)
    except UnicodeEncodeError:
        click.echo(msg.encode("ascii", errors="replace").decode("ascii"), err=True)


@click.group()
@click.option(
    "--verbose/--quiet",
    "verbose",
    default=None,
    help="Show full progress narration. Quiet (results only) is the default everywhere.",
)
@click.version_option(__version__, prog_name="sa")
def main(verbose: bool | None) -> None:
    """PSR.ai - turn Windows Steps Recorder output into AI-ready documentation."""
    _ensure_utf8_stdout()
    # Without an explicit flag the environment decides, re-read on every
    # invocation so PSR_AI_QUIET / PSR_AI_VERBOSE work regardless of when this
    # module happened to be imported.
    _set_verbosity(_initial_verbosity() if verbose is None else verbose)


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
@click.option(
    "--low-token",
    is_flag=True,
    default=False,
    help="Downscale screenshots to 512px (max dimension) to cut LLM vision token costs.",
)
@click.option(
    "--context/--no-context",
    "use_context",
    default=True,
    show_default=True,
    help=(
        "Run the Hidden Context Layer listener so the browser extension can "
        "attach failed requests, console errors, and clicked-element data."
    ),
)
def start(output: str | None, maxsc: int, low_token: bool, use_context: bool) -> None:
    """Start a new PSR recording session.

    Recording runs in the background - your terminal is free immediately.
    Run `sa stop` when you are done to save the recording.

    Quiet by default: the only thing printed is the path the session will be
    written to. Use `sa --verbose start` for the full breakdown.

    The engine is chosen silently: psr.exe when Windows allows it, otherwise a
    detached local polling daemon using native OS screen capture.
    """
    try:
        recorder = Recorder()
    except PSRNotFoundError as exc:
        _warn(f"Error: {exc}")
        raise SystemExit(1)

    max_dim = 512 if low_token else None
    try:
        out_path = recorder.start_recording(
            output_path=output, max_screenshots=maxsc, max_dim=max_dim
        )
    except OSError as exc:
        _warn(f"Error: could not start a recording ({exc}).")
        raise SystemExit(1)

    _detail("Recording started.")
    _detail(f"  Engine: {type(recorder).__name__}")
    _detail(f"  Max screenshots: {maxsc}")
    if low_token:
        _detail("  Low-token mode: screenshots capped at 512px")

    if use_context:
        listener_port = _start_context_listener(out_path)
        if listener_port:
            _detail(f"  Context layer: listening on 127.0.0.1:{listener_port}")
            _report_context_layer(listener_port)
        else:
            _detail("  Context layer: unavailable (recording continues without it)")

    _detail("  Run `sa stop` when finished.")

    # The one essential result: where this session lands.
    _echo(str(out_path))


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
@click.option(
    "--clipboard",
    "include_clipboard",
    is_flag=True,
    default=False,
    help=(
        "Also capture the current clipboard text into data.json. Off by default: "
        "clipboards routinely hold credentials. Captured text is redacted and truncated."
    ),
)
def stop(skip_gif: bool, include_clipboard: bool) -> None:
    """Stop the active recording session and save the ZIP.

    Waits for the engine to finish writing the file before returning.
    After saving, automatically parses the ZIP, renders report.md,
    and builds action_replay.gif (pass --no-gif to skip the GIF).

    Quiet by default: one line with the step count and the report path.
    Use `sa --verbose stop` to see every intermediate artifact.
    """
    try:
        recorder = Recorder()
    except PSRNotFoundError as exc:
        _warn(f"Error: {exc}")
        raise SystemExit(1)

    # Signal the context listener first so it stops accepting events and exits
    # while the ZIP is still being parsed.
    _stop_context_listener()

    try:
        zip_path = recorder.stop_recording()
    except OSError as exc:
        _warn(f"Error: could not stop the recording ({exc}).")
        raise SystemExit(1)

    if zip_path is None or not zip_path.exists():
        _warn("No recording output found - the session may have exited before saving.")
        raise SystemExit(1)

    size_kb = zip_path.stat().st_size // 1024
    _detail("Recording stopped.")
    _detail(f"  File: {zip_path}")
    _detail(f"  Size: {size_kb} KB")

    # Auto-parse: extract steps, dump data.json, render report.md, build GIF.
    try:
        session = parse_session(zip_path)
        output_dir = default_output_dir(zip_path)
        json_path = generate_json_meta(session, output_dir / "data.json")

        # Enrich data.json before the report is rendered, so report.md and any
        # downstream consumer see the same enriched document.
        _enrich_with_context(json_path, output_dir, include_clipboard)

        report_path = MarkdownGenerator(json_path).write(output_dir)

        _detail(f"  Parsed:      {len(session.steps)} steps extracted")
        _detail(f"  Data:        {json_path}")
        _detail(f"  Screenshots: {output_dir / 'screenshots'}")
        _detail(f"  Report:      {report_path}")

        if not skip_gif:
            _build_gif_for_stop(session, output_dir, json_path)

        _enrich_with_html(session, json_path)
        _suggest_video_analysis()

        # The one essential result: how much was captured, and where to read it.
        _echo(f"{len(session.steps)} steps -> {report_path}")

    except Exception as exc:
        _warn(f"Warning: auto-parse failed ({exc}). The ZIP was saved successfully.")
        _warn(f"Run `sa pub {zip_path}` to generate documentation manually.")
        _echo(str(zip_path))


# ------------------------------------------------------------------
# Hidden Context Layer helpers
# ------------------------------------------------------------------

def _start_context_listener(zip_path: Path) -> int | None:
    """Spawn the detached context listener for this session. Returns its port.

    Returns None when the listener cannot be started - a missing context layer
    must never prevent a recording.
    """
    import subprocess
    from psr_ai.core import context_server

    session_dir = default_output_dir(Path(zip_path))
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        token = context_server.ensure_token()
    except OSError:
        return None

    # A listener left over from a crashed session would hold the port.
    context_server.request_stop()

    cmd = [
        sys.executable, "-m", "psr_ai", "context", "serve",
        "--session-dir", str(session_dir),
        "--session-id", Path(zip_path).stem,
        "--port", str(context_server.DEFAULT_PORT),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            cwd=str(session_dir),
        )
    except OSError:
        return None

    del token   # Only ensured so the extension has something to pair against.
    return context_server.DEFAULT_PORT


def _report_context_layer(port: int) -> None:
    """Tell the user, in one line, whether the browser layer is actually live.

    The listener has only just started, so the extension may not have checked
    in yet - a brief wait avoids reporting a false negative on every run.
    """
    import time
    from psr_ai.core import context_server

    token = context_server.read_token()
    deadline = time.monotonic() + 2.0
    health = None
    while time.monotonic() < deadline:
        health = context_server.probe_listener(port=port, token=token)
        if health and health.get("extension_seen"):
            break
        time.sleep(0.25)

    if health and health.get("extension_seen"):
        client = health.get("client") or {}
        who = client.get("browser") or "browser"
        _detail(f"  Context layer: live ({who})")
    else:
        # Not an error: the browser may simply not be open yet.
        _detail("  Context layer: waiting for the browser extension")
        _detail("                 check with: sa context doctor")


def _stop_context_listener() -> None:
    """Ask a running context listener to shut down. Never raises."""
    try:
        from psr_ai.core import context_server
        context_server.request_stop()
    except Exception:
        pass


def _warn_if_layer_never_seen(output_dir: Path, merged: int) -> None:
    """Warn when a session captured no browser context and none was possible.

    Silence used to be ambiguous: a clean run and a switched-off extension
    produced identical output. The liveness record tells them apart, so the
    user is only nagged when something really was wrong.
    """
    if merged:
        return
    try:
        from psr_ai.core import context_server
        meta = context_server.read_context_meta(output_dir)
    except Exception:
        return

    if meta and meta.get("last_seen"):
        return   # The layer was live; the page genuinely had nothing to report.

    _warn("Note: no browser context was captured - the extension never checked in.")
    _warn("      Run `sa context doctor` to see why (it is usually disabled in chrome://extensions).")


def _enrich_with_context(json_path: Path, output_dir: Path, include_clipboard: bool) -> None:
    """Merge browser context and OS metadata into data.json."""
    try:
        from psr_ai.core import context_server, os_context
    except ImportError:
        return

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    merged = 0
    try:
        merged = context_server.merge_context_into_data(data, output_dir)
    except Exception:
        pass   # Context is a bonus; never let it break `sa stop`.

    try:
        os_context.enrich_data_json(data, include_clipboard=include_clipboard)
    except Exception:
        pass

    try:
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return

    if merged:
        summary = data.get("browser_context", {})
        kinds = summary.get("by_kind", {})
        _detail(
            f"  Browser ctx: {summary.get('events_total', 0)} events "
            f"({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})"
        )
    else:
        _warn_if_layer_never_seen(output_dir, merged)
    active_window = data.get("os_context", {}).get("active_window")
    if active_window:
        _detail(f"  Active window: {active_window[:70]}")
    if include_clipboard:
        _detail("  Clipboard:   captured (redacted)")


def _enrich_with_html(session, json_path: Path) -> None:
    """Extract element HTML from browser steps and write back to data.json."""
    try:
        from psr_ai.core.html_extractor import enrich_steps_with_html
        data = json.loads(json_path.read_text(encoding="utf-8"))
        enriched = enrich_steps_with_html(data.get("steps", []), echo=_detail)
        if enriched:
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            _detail(f"  HTML context: {enriched} element(s) extracted from browser")
    except Exception:
        pass  # HTML extraction is best-effort — never block sa stop


def _suggest_video_analysis() -> None:
    """After sa stop: detect open video windows and print a one-line hint."""
    try:
        from psr_ai.core.window_detect import detect_video_windows
        videos = detect_video_windows()
        if not videos:
            return
        v = videos[0]
        _detail(f"  Video detected: \"{v.title[:70]}\"")
        _detail(f"  → sa video-analyze \"{v.suggest_url}\"")
        if len(videos) > 1:
            _detail(f"  ({len(videos) - 1} more video window(s) detected)")
    except Exception:
        pass


def _build_gif_for_stop(session, output_dir: Path, json_path: Path) -> None:
    """Build action_replay.gif and print status. Called only from sa stop."""
    try:
        gif_gen = GifGenerator(
            data_json_path=json_path,
            screenshots_dir=output_dir / "screenshots",
        )
        gif_path = gif_gen.write(output_dir)
        _detail(f"  GIF:         {gif_path}")
    except ImportError:
        _detail("  GIF skipped: Pillow not installed. Run: pip install psr-ai[gif]")
    except ValueError as exc:
        # No frames found - session had no screenshots. Non-fatal.
        _detail(f"  GIF skipped: {exc}")


# ------------------------------------------------------------------
# sa record-daemon  (internal - spawned detached by sa start)
# ------------------------------------------------------------------

@main.command("record-daemon", hidden=True)
@click.option("--staging", required=True, type=click.Path(), help="Staging directory.")
@click.option("--zip", "zip_path", required=True, type=click.Path(), help="Output ZIP path.")
@click.option("--maxsc", default=100, show_default=True, help="Hard frame cap.")
@click.option("--interval", default=3.0, show_default=True, help="Seconds between frames.")
@click.option("--max-dim", default=None, type=int, help="Downscale frames to this longest edge.")
def record_daemon(
    staging: str,
    zip_path: str,
    maxsc: int,
    interval: float,
    max_dim: int | None,
) -> None:
    """Run one polling recording to completion. Not meant to be called by hand.

    `sa start` spawns this detached when psr.exe is unavailable. It exits on
    its own once the frame cap is reached or `sa stop` drops the stop sentinel,
    packing the ZIP either way. It prints nothing: stdout and stderr belong to
    a detached process with no console.
    """
    from psr_ai.core.recorder import run_polling_session

    try:
        run_polling_session(
            staging_dir=Path(staging),
            zip_path=Path(zip_path),
            max_screenshots=maxsc,
            interval=interval,
            max_dim=max_dim,
        )
    except Exception:
        raise SystemExit(1)


# ------------------------------------------------------------------
# sa context  (Hidden Context Layer)
# ------------------------------------------------------------------

@main.group()
def context() -> None:
    """Hidden Context Layer: browser context without an open DevTools window.

    A Manifest V3 companion extension silently captures failed requests
    (4xx/5xx), uncaught console errors, and the accessibility snapshot of the
    element you click, then posts them to this local listener while a recording
    session is active. Nothing is transmitted between sessions.

    Everything ships inside psr-ai - there is nothing to install from a store
    and nothing to pay for.

      sa context install   open a browser with the layer already loaded
      sa context browser   same thing, with browser/URL choice
      sa context token     show (or rotate) the pairing token
      sa context status    is the listener running? is it recording?
      sa context events    dump what has been captured (JSON, for any tool)
    """


def _extension_dir() -> Path:
    """Return the bundled extension directory (source tree or installed wheel)."""
    import importlib.resources
    try:
        return Path(str(importlib.resources.files("psr_ai") / "extension"))
    except (TypeError, AttributeError, ModuleNotFoundError):
        return Path(__file__).resolve().parent / "extension"


@context.command("browser")
@click.option("--browser", "-b", "preferred", default=None,
              help="Which browser to launch: chrome, edge, or brave. Default: first found.")
@click.option("--url", "-u", default=None, help="URL to open in the instrumented browser.")
def context_browser(preferred: str | None, url: str | None) -> None:
    """Open a browser with the context layer already loaded - nothing to install.

    Uses a dedicated PSR.ai profile, so your everyday browser is untouched and
    logins here persist between sessions. This is the zero-install path: no
    Chrome Web Store, no developer-mode clicking, no fees.

    Examples:
      sa context browser
      sa context browser --browser edge
      sa context browser --url https://myapp.local/checkout
    """
    from psr_ai.core import context_browser as cb

    try:
        browser, pid = cb.launch(preferred=preferred, url=url)
    except cb.UnsupportedBrowserError as exc:
        _warn(f"Error: {exc}")
        raise SystemExit(1)
    except FileNotFoundError as exc:
        _warn(f"Error: {exc}")
        raise SystemExit(1)
    except OSError as exc:
        _warn(f"Error: could not start the browser ({exc}).")
        raise SystemExit(1)

    _detail(f"  Executable: {browser.executable}")
    _detail(f"  Profile:    {browser.profile_dir}")
    _detail(f"  PID:        {pid}")
    _echo(f"{browser.name} launched with the context layer loaded.")


@context.command("install")
@click.option("--launch/--no-launch", "do_launch", default=True, show_default=True,
              help="Open an instrumented browser immediately instead of only printing steps.")
@click.option("--browser", "-b", "preferred", default=None,
              help="Which browser to launch: chrome, edge, or brave.")
def context_install(do_launch: bool, preferred: str | None) -> None:
    """Set up the context layer - by default it just opens a ready browser.

    There is nothing to install from a store and nothing to pay for. The
    extension ships inside the psr-ai package, and this command starts a
    browser that already has it loaded.

    Use --no-launch to get the manual instructions for loading the same
    directory into your own everyday browser profile instead.
    """
    from psr_ai.core import context_browser as cb, context_server

    ext_dir = _extension_dir()
    if not (ext_dir / "manifest.json").exists():
        _warn(f"Error: extension files not found at {ext_dir}")
        raise SystemExit(1)

    token = context_server.ensure_token()

    if do_launch:
        try:
            browser, pid = cb.launch(preferred=preferred)
        except (cb.UnsupportedBrowserError, FileNotFoundError, OSError) as exc:
            _warn(f"Could not launch a browser automatically: {exc}")
            _warn("Falling back to manual setup instructions.")
            do_launch = False
        else:
            _detail(f"  Executable: {browser.executable}")
            _detail(f"  Profile:    {browser.profile_dir}")
            _detail(f"  PID:        {pid}")
            _detail(f"  Extension:  {ext_dir}")
            _echo(f"{browser.name} is open with the context layer loaded. Run `sa start` and work normally.")
            return

    _echo("PSR.ai Hidden Context Layer - manual setup")
    _echo("")
    _echo("1. Open  chrome://extensions  (or edge://extensions)")
    _echo("2. Turn on 'Developer mode' (top right)")
    _echo("3. Click 'Load unpacked' and choose:")
    _echo(f"     {ext_dir}")
    _echo("4. Open the extension's options and paste this pairing token:")
    _echo(f"     {token}")
    _echo("")
    _echo(f"Listener endpoint: http://127.0.0.1:{context_server.DEFAULT_PORT}")
    _echo("Then run `sa start` - capture begins automatically, no DevTools needed.")


@context.command("token")
@click.option("--rotate", is_flag=True, default=False, help="Generate a new token, invalidating the old one.")
def context_token(rotate: bool) -> None:
    """Print the pairing token the extension must send."""
    from psr_ai.core import context_server

    token = context_server.ensure_token(rotate=rotate)
    if rotate:
        _detail("Token rotated - re-paste it in the extension options.")
    _detail(f"Stored at: {context_server.token_path()}")
    _echo(token)


@context.command("status")
def context_status() -> None:
    """Show whether the context listener is running and what it has collected."""
    from psr_ai.core import context_server

    state = context_server.read_state()
    if not state:
        _echo("Context layer: not running")
        return

    session_dir = Path(state.get("session_dir", ""))
    events = len(context_server.load_context_events(session_dir)) if session_dir else 0
    _detail(f"  PID:     {state.get('pid')}")
    _detail(f"  Session: {state.get('session_id')}")
    _detail(f"  Dir:     {session_dir}")
    _echo(f"Context layer: listening on 127.0.0.1:{state.get('port')} · {events} events captured")


@context.command("doctor")
def context_doctor() -> None:
    """Say plainly whether the context layer is working, and what to fix.

    Checks every link in the chain and gives each a verdict. A silent session
    is ambiguous - "the page had no errors" and "the extension is switched off"
    both produce zero events - so this command exists to remove the ambiguity.

    Exits 1 when something is broken, so it can gate a script.
    """
    from psr_ai.core import context_browser, context_server

    problems: list[str] = []

    def line(label: str, verdict: str, detail: str = "") -> None:
        _echo(f"{label:<14}{verdict:<7}{detail}")

    # 1. Listener ------------------------------------------------------
    state = context_server.read_state()
    port = int(state.get("port", context_server.DEFAULT_PORT)) if state else context_server.DEFAULT_PORT
    token = context_server.read_token()
    health = context_server.probe_listener(port=port, token=token)

    if health is None:
        line("Listener", "DOWN", "nothing answering on 127.0.0.1:" f"{port}")
        _echo(f"{'':14}       -> start a recording: sa start")
        problems.append("listener")
    elif health.get("error") == "unauthorized":
        line("Listener", "AUTH", f"running on 127.0.0.1:{port}, but rejected our token")
        _echo(f"{'':14}       -> re-pair the extension: sa context token")
        problems.append("token")
    elif health.get("error"):
        line("Listener", "ERROR", str(health.get("error")))
        problems.append("listener")
    else:
        line("Listener", "OK", f"127.0.0.1:{port}")

    healthy = bool(health) and not health.get("error")

    # 2. Extension liveness --------------------------------------------
    if healthy:
        seen_ago = health.get("extension_seen_seconds_ago")
        client = health.get("client") or {}
        who = " ".join(filter(None, [client.get("browser"), client.get("version")])) or "unknown browser"
        if health.get("extension_live"):
            line("Extension", "OK", f"{who}, checked in {seen_ago:.0f}s ago")
        elif health.get("extension_seen"):
            line("Extension", "STALE", f"{who}, last seen {seen_ago:.0f}s ago")
            _echo(f"{'':14}       -> the browser may be closed, or the extension was disabled")
            problems.append("extension")
        else:
            line("Extension", "DOWN", "never checked in during this session")
            _echo(f"{'':14}       -> open chrome://extensions and make sure")
            _echo(f"{'':14}          'PSR.ai Hidden Context Layer' is enabled")
            _echo(f"{'':14}       -> not installed yet? run: sa context install --no-launch")
            problems.append("extension")

    # 3. Session and events ---------------------------------------------
    if healthy and state:
        session_dir = Path(state.get("session_dir", ""))
        line("Session", "OK", str(health.get("session_id") or state.get("session_id")))
        events = context_server.load_context_events(session_dir)
        counts: dict[str, int] = {}
        for event in events:
            counts[str(event.get("kind"))] = counts.get(str(event.get("kind")), 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none yet"
        line("Events", str(len(events)), detail)
    elif healthy:
        line("Session", "IDLE", "listener up but not attached to a recording")

    # 4. Browsers -------------------------------------------------------
    browsers = context_browser.find_browsers()
    if browsers:
        detail = ", ".join(
            f"{b.name}{'' if b.supports_auto_load else ' (manual load only)'}" for b in browsers
        )
        line("Browsers", "OK", detail)
    else:
        line("Browsers", "NONE", "no Chromium-family browser found")
        problems.append("browser")

    if problems:
        _echo("")
        _echo(f"Not ready: {', '.join(sorted(set(problems)))}. Fix the arrows above.")
        raise SystemExit(1)
    _echo("")
    _echo("Context layer is working.")


@context.command("test")
@click.option("--timeout", default=25.0, show_default=True, help="Seconds to wait for events.")
@click.option("--keep-open", is_flag=True, default=False, help="Leave the test page open afterwards.")
def context_test(timeout: float, keep_open: bool) -> None:
    """Prove the whole loop works, end to end, in about ten seconds.

    Serves a local page that deliberately fails a request and logs a console
    error, opens it in your browser, and reports which signals actually
    arrived. This is the difference between believing it works and knowing.

    A recording session must be active (run `sa start` first) because the
    listener drops everything when nothing is being recorded.
    """
    from psr_ai.core import context_server
    from psr_ai.core.context_selftest import run_self_test

    state = context_server.read_state()
    if not state:
        _warn("No active recording session - the listener drops context when idle.")
        _warn("Run `sa start` first, then `sa context test`.")
        raise SystemExit(1)

    _detail("Serving a page that fails a request and logs a console error...")
    result = run_self_test(
        session_dir=Path(state.get("session_dir", "")),
        timeout=timeout,
        keep_open=keep_open,
        echo=_detail,
    )

    for label, ok in result.checks.items():
        _echo(f"  [{'PASS' if ok else 'FAIL'}]  {label}")

    _echo("")
    if result.passed:
        _echo(f"Context layer verified: {result.score}.")
        return

    _echo(f"Context layer incomplete: {result.score}.")
    for hint in result.hints:
        _warn(f"  {hint}")
    raise SystemExit(1)


@context.command("events")
@click.option("--session-dir", default=None, type=click.Path(),
              help="Session directory to read. Defaults to the live session.")
@click.option("--kind", default=None,
              type=click.Choice(["network_failure", "console_error", "resource_error", "element_click"]),
              help="Only show events of this kind.")
@click.option("--limit", default=0, type=int, help="Show at most N events (0 = all).")
@click.option("--summary", is_flag=True, default=False, help="Print counts per kind instead of events.")
def context_events(session_dir: str | None, kind: str | None, limit: int, summary: bool) -> None:
    """Dump captured browser context as JSON on stdout.

    This is the integration point for any tool that is not MCP-aware: pipe it
    into jq, into a script, or straight into an agent prompt.

    Examples:
      sa context events
      sa context events --kind network_failure
      sa context events --summary
      sa context events --session-dir psr_output/psr_20260804_120000_output
    """
    from psr_ai.core import context_server

    if session_dir:
        target = Path(session_dir)
    else:
        state = context_server.read_state()
        if not state:
            _warn("No live session. Pass --session-dir to read a finished one.")
            raise SystemExit(1)
        target = Path(state.get("session_dir", ""))

    events = context_server.load_context_events(target)
    if kind:
        events = [e for e in events if e.get("kind") == kind]
    if limit > 0:
        events = events[-limit:]

    if summary:
        counts: dict[str, int] = {}
        for event in events:
            counts[str(event.get("kind"))] = counts.get(str(event.get("kind")), 0) + 1
        _echo(json.dumps({"session_dir": str(target), "total": len(events), "by_kind": counts}, indent=2))
        return

    _echo(json.dumps(events, indent=2, ensure_ascii=False))


@context.command("serve")
@click.option("--session-dir", required=True, type=click.Path(), help="Directory to write context.jsonl into.")
@click.option("--session-id", default=None, help="Identifier reported to the extension.")
@click.option("--port", default=None, type=int, help="Port to bind on 127.0.0.1.")
@click.option("--stop-file", default=None, type=click.Path(), help="Sentinel path that ends the listener.")
@click.option("--foreground", is_flag=True, default=False, help="Log bind details instead of staying silent.")
def context_serve(
    session_dir: str,
    session_id: str | None,
    port: int | None,
    stop_file: str | None,
    foreground: bool,
) -> None:
    """Run the context listener until stopped. Normally started by `sa start`.

    Blocks. Ends when `sa stop` drops the stop sentinel.
    """
    from psr_ai.core import context_server

    token = context_server.ensure_token()
    if foreground:
        _echo(f"Listening on 127.0.0.1:{port or context_server.DEFAULT_PORT}")
        _echo(f"  Session dir: {session_dir}")
        _echo("  Ctrl-C to stop.")

    try:
        context_server.serve_context_session(
            session_dir=Path(session_dir),
            session_id=session_id,
            port=port or context_server.DEFAULT_PORT,
            token=token,
            stop_file=Path(stop_file) if stop_file else None,
        )
    except OSError as exc:
        _warn(f"Error: context listener could not bind ({exc}).")
        raise SystemExit(1)


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


# Hebrew alias: sa צלם — only register on terminals that support Unicode
try:
    "צלם".encode(click.utils._default_text_stdout().encoding or "utf-8")
    main.add_command(capture, name="צלם")
except (UnicodeEncodeError, Exception):
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

@main.group(invoke_without_command=True)
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Start the PSR.ai MCP JSON-RPC server on stdin/stdout.

    Blocks the terminal and keeps stdin/stdout reserved for full-duplex
    communication with an MCP host (e.g. Claude Desktop, Claude Code).

    The server speaks JSON-RPC 2.0 over newline-delimited messages.
    Send Ctrl-C or close stdin to stop it.

    Tools exposed:
      psr_start_recording  - start a recording session
      psr_stop_and_parse   - stop, parse, and return the session report
      psr_quick_snapshot   - capture and return an optimized Base64 screenshot

    Subcommands:
      sa mcp install   - register this server with Claude Desktop
      sa mcp path      - print the Claude Desktop config path

    Example (manual test):
      echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | sa mcp
    """
    if ctx.invoked_subcommand is not None:
        return
    from psr_ai.mcp import main_loop
    main_loop()


# Default server key written into claude_desktop_config.json.
_MCP_SERVER_KEY = "psr-ai"


def _claude_desktop_config_path() -> Path:
    """Return the Claude Desktop config path for the current OS.

    Windows : %APPDATA%\\Claude\\claude_desktop_config.json
    macOS   : ~/Library/Application Support/Claude/claude_desktop_config.json
    Linux   : $XDG_CONFIG_HOME/Claude/claude_desktop_config.json
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        config_dir = base / "Claude"
    elif sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        config_dir = base / "Claude"
    return config_dir / "claude_desktop_config.json"


def _mcp_server_block() -> dict:
    """Build the MCP server block, resolved against the *current* environment.

    Every path is absolute and derived from the running interpreter, so inside
    a venv the block points at that venv - no PATH lookup, and no "works in my
    shell but not in Claude Desktop". The `sa` console script is preferred when
    it can be located; otherwise `python -m psr_ai` is used, which needs only
    the interpreter itself.
    """
    import sysconfig

    exe_dir = Path(sys.executable).parent
    script_name = "sa.exe" if sys.platform == "win32" else "sa"
    candidates = [
        Path(sysconfig.get_path("scripts")) / script_name,   # venv or base install
        exe_dir / script_name,                               # venv layout on POSIX
        exe_dir / "Scripts" / script_name,                   # base install on Windows
        exe_dir / "bin" / script_name,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return {"command": str(candidate), "args": ["mcp"]}
        except OSError:
            continue
    return {"command": sys.executable, "args": ["-m", "psr_ai", "mcp"]}


def _load_json_config(path: Path) -> dict:
    """Read a JSON config file, tolerating a UTF-8 BOM. Missing file -> {}."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return data


@mcp.command("path")
def mcp_path() -> None:
    """Print the Claude Desktop config path for this machine."""
    _echo(str(_claude_desktop_config_path()))


@mcp.command("install")
@click.option(
    "--config", "config_path",
    default=None,
    type=click.Path(),
    help="Config file to modify. Defaults to the OS Claude Desktop location.",
)
@click.option(
    "--name",
    default=_MCP_SERVER_KEY,
    show_default=True,
    help="Key to register the server under in mcpServers.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing entry with the same name.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the block that would be written and exit.",
)
def mcp_install(config_path: str | None, name: str, force: bool, dry_run: bool) -> None:
    """Register the PSR.ai MCP server with Claude Desktop.

    Auto-detects the config location per OS, resolves the interpreter of the
    current virtualenv, and merges the server into mcpServers. Other servers
    already in the file are preserved untouched; the previous file is copied
    to <name>.json.bak before anything is written.

    Examples:
      sa mcp install
      sa mcp install --dry-run
      sa mcp install --force
      sa mcp install --config ~/custom/claude_desktop_config.json
    """
    target = Path(config_path).expanduser() if config_path else _claude_desktop_config_path()
    block = _mcp_server_block()

    if dry_run:
        _detail(f"Config: {target}")
        _echo(json.dumps({"mcpServers": {name: block}}, indent=2))
        return

    try:
        config = _load_json_config(target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _warn(f"Error: {target} is not readable as JSON ({exc}).")
        _warn("Fix or move the file, then re-run `sa mcp install`.")
        raise SystemExit(1)

    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        # Replacing it would silently discard whatever the user had there.
        _warn(f"Error: 'mcpServers' in {target} is not an object - refusing to overwrite it.")
        raise SystemExit(1)
    existing = servers.get(name)

    if existing == block:
        _echo(f"Already installed: {name} -> {target}")
        return
    if existing is not None and not force:
        _warn(f"Error: '{name}' already exists in {target}. Re-run with --force to replace it.")
        raise SystemExit(1)

    servers[name] = block
    config["mcpServers"] = servers

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            import shutil
            backup = target.with_suffix(target.suffix + ".bak")
            # Byte-for-byte, so the backup restores the original exactly
            # (encoding, BOM, and formatting included).
            shutil.copy2(target, backup)
            _detail(f"  Backup: {backup}")
        # Write to a temp file in the same directory, then swap it in, so an
        # interrupted write can never truncate a working config.
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        _warn(f"Error: could not write {target} ({exc}).")
        raise SystemExit(1)

    _detail(f"  Command: {block['command']} {' '.join(block['args'])}")
    _detail(f"  Servers in file: {', '.join(sorted(servers))}")
    _echo(f"Installed: {name} -> {target}")
    _detail("Restart Claude Desktop to load the server.")


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
    "--max-frames",
    default=100,
    type=int,
    show_default=True,
    help="Maximum number of frames to extract (auto-fps scales to fit budget).",
)
@click.option(
    "--start",
    "start_seconds",
    default=None,
    type=float,
    help="Focus range start in seconds (enables denser frame sampling).",
)
@click.option(
    "--end",
    "end_seconds",
    default=None,
    type=float,
    help="Focus range end in seconds.",
)
@click.option(
    "--resolution",
    default=512,
    type=int,
    show_default=True,
    help="Frame width in pixels. Higher = more detail but more tokens.",
)
def video_analyze(
    source: str,
    question: str | None,
    output: str | None,
    max_frames: int,
    start_seconds: float | None,
    end_seconds: float | None,
    resolution: int,
) -> None:
    """Analyze a video using frames + audio transcription + Claude.

    SOURCE can be a URL (YouTube, TikTok, Instagram Reel, Twitter/X, etc.)
    or a local video file path.

    Frame rate is automatically scaled to fit the frame budget (short videos
    get dense sampling, long videos get sparse). Use --start/--end to focus
    on a specific section with higher density.

    Requires: pip install psr-ai[analyze]

    Examples:
      sa video-analyze https://www.youtube.com/watch?v=abc123
      sa video-analyze lecture.mp4 -q "summarize the key points"
      sa video-analyze video.mp4 --start 60 --end 120 -q "what happens here?"
      sa video-analyze clip.mp4 --resolution 1024 -q "read the text on screen"
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
            max_frames=max_frames,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            resolution=resolution,
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
