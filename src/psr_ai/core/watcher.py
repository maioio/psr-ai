"""Screen watcher: monitors the desktop continuously and reacts when a trigger is detected.

Claude inspects every screenshot with a yes/no question: "Has this happened?"
When the answer is yes, an action agent runs to respond.

Trigger types:
  - Visual pattern: describe what to look for ("error dialog appeared",
    "build finished", "download complete badge visible")
  - Text match:     a string that must appear on screen (uses Claude OCR)

Usage:
    from psr_ai.core.watcher import WatchConfig, run_watcher
    config = WatchConfig(
        trigger="a red error dialog appeared on screen",
        action="close the error dialog and note the error message",
        interval=5.0,
        dry_run=False,
    )
    result = run_watcher(config)
    print(result.report())
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Config and result
# ---------------------------------------------------------------------------

@dataclass
class WatchConfig:
    """Parameters for a single watcher run."""

    trigger: str                          # what to watch for (natural language)
    action: str                           # what to do when trigger fires
    interval: float = 5.0                 # seconds between screen checks
    timeout: float = 3600.0               # stop after this many seconds total
    max_triggers: int = 1                 # how many times to react before stopping
    provider: str = "anthropic"
    model: str | None = None
    dry_run: bool = True
    ask_before_action: bool = False       # default: automatic
    ask_before_destructive: bool = True
    output_dir: Path | None = None


@dataclass
class TriggerEvent:
    """One detected trigger occurrence."""

    index: int
    timestamp: float
    reasoning: str                        # Claude's explanation for the detection
    agent_success: bool = False
    agent_stopped_reason: str = ""


@dataclass
class WatchResult:
    """Outcome of a run_watcher() call."""

    config: WatchConfig
    success: bool                         # True if at least one trigger was handled
    triggers_detected: int = 0
    checks_performed: int = 0
    elapsed_seconds: float = 0.0
    events: list[TriggerEvent] = field(default_factory=list)
    stopped_reason: str = ""              # "max_triggers" | "timeout" | "user_interrupt" | "error"
    error: str = ""

    def report(self) -> str:
        status = "SUCCESS" if self.success else "FAILED" if self.error else "TIMEOUT"
        lines = [
            f"Watch result: {status}",
            f"  Trigger:    {self.config.trigger}",
            f"  Checks:     {self.checks_performed}",
            f"  Detections: {self.triggers_detected}",
            f"  Elapsed:    {self.elapsed_seconds:.1f}s",
            f"  Stopped:    {self.stopped_reason}",
        ]
        for ev in self.events:
            ts = time.strftime("%H:%M:%S", time.localtime(ev.timestamp))
            outcome = "ok" if ev.agent_success else ev.agent_stopped_reason
            lines.append(f"  [{ts}] Trigger #{ev.index}: {ev.reasoning[:80]} -> {outcome}")
        if self.error:
            lines.append(f"  Error:      {self.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_watcher(
    config: WatchConfig,
    echo_fn: Callable[[str], None] | None = None,
    prompt_fn: Callable[[str], str] | None = None,
) -> WatchResult:
    """Monitor the screen and react when the trigger condition is detected.

    Never raises. All exceptions are caught and written to result.error.

    Args:
      config:    WatchConfig controlling behaviour.
      echo_fn:   callable for progress output. Defaults to print().
      prompt_fn: callable for user prompts passed to the action agent.

    Returns WatchResult with all detected trigger events.
    """
    echo = echo_fn or print
    result = WatchResult(config=config, success=False)

    try:
        _run(config, result, echo, prompt_fn)
    except KeyboardInterrupt:
        result.stopped_reason = "user_interrupt"
        result.error = "Interrupted by user (Ctrl+C)."
        echo("\nWatcher interrupted. Stopping cleanly.")
    except Exception as exc:
        result.stopped_reason = "error"
        result.error = str(exc)
        echo(f"Watcher error: {exc}")

    return result


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
}

_KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_DETECT_SYSTEM = """\
You are a screen-monitoring assistant. Your only job is to answer YES or NO.

The user is watching for a specific condition on their screen.
Look at the screenshot carefully and decide whether the condition is currently met.

Condition to detect: {trigger}

Respond with ONLY a JSON object:
{{
  "detected": true or false,
  "reasoning": "one sentence explaining what you see and why you answered yes or no"
}}

No markdown. No extra text. JSON only.
"""


def _get_executor():
    """Thin wrapper so tests can patch at psr_ai.core.watcher._get_executor."""
    from psr_ai.core.executor import get_executor
    return get_executor()


def _check_trigger(
    config: WatchConfig,
    model: str,
    img_bytes: bytes,
) -> tuple[bool, str]:
    """Ask Claude whether the trigger condition is visible in the screenshot.

    Returns (detected: bool, reasoning: str).
    """
    import json as _json

    from psr_ai.core.agent_prompt import resize_screenshot_bytes

    optimized = resize_screenshot_bytes(img_bytes)
    b64 = base64.b64encode(optimized).decode("ascii")
    media_type = "image/jpeg" if optimized != img_bytes else "image/png"

    system = _DETECT_SYSTEM.format(trigger=config.trigger)
    user_content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": f"Is this condition currently visible on screen: {config.trigger}"},
    ]
    messages = [{"role": "user", "content": user_content}]

    if config.provider == "anthropic":
        raw = _call_anthropic(model, system, messages)
    elif config.provider == "openai":
        raw = _call_openai(model, system, messages)
    else:
        raise ValueError(f"Unknown provider: {config.provider!r}")

    # Parse yes/no response.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.splitlines() if not l.startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data = _json.loads(cleaned)
        return bool(data.get("detected", False)), str(data.get("reasoning", ""))
    except (_json.JSONDecodeError, AttributeError):
        # Fallback: look for yes/no in plain text.
        lower = raw.lower()
        detected = "yes" in lower or '"detected": true' in lower
        return detected, raw[:120]


def _call_anthropic(model: str, system: str, messages: list) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package required. Install with: pip install psr-ai[agent]"
        ) from exc
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model, max_tokens=256, system=system, messages=messages,
    )
    return response.content[0].text


def _call_openai(model: str, system: str, messages: list) -> str:
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package required. Install with: pip install psr-ai[ai]"
        ) from exc
    full_messages = [{"role": "system", "content": system}] + messages
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model, max_tokens=256, messages=full_messages,
    )
    return response.choices[0].message.content


def _run_action_agent(
    config: WatchConfig,
    echo: Callable[[str], None],
    prompt_fn: Callable[[str], str] | None,
) -> tuple[bool, str]:
    """Run the action agent in response to a detected trigger.

    Returns (success, stopped_reason).
    """
    from psr_ai.core.agent import AgentConfig, run_agent

    agent_config = AgentConfig(
        task=config.action,
        provider=config.provider,
        model=config.model,
        dry_run=config.dry_run,
        ask_before_action=config.ask_before_action,
        ask_before_destructive=config.ask_before_destructive,
        max_steps=20,
        output_dir=config.output_dir,
    )
    ar = run_agent(agent_config, echo_fn=echo, prompt_fn=prompt_fn)
    return ar.success, ar.stopped_reason


def _run(
    config: WatchConfig,
    result: WatchResult,
    echo: Callable[[str], None],
    prompt_fn: Callable[[str], str] | None,
) -> None:
    # Pre-flight API key check.
    key_var = _KEY_VARS.get(config.provider, "")
    if key_var and not os.environ.get(key_var):
        raise ValueError(
            f"{key_var} environment variable is not set. "
            f"Export it before running `sa watch --provider {config.provider}`."
        )

    model = config.model or _DEFAULT_MODELS.get(config.provider, "")
    executor = _get_executor()

    if config.dry_run:
        echo("DRY RUN mode: trigger detection is live but action will NOT be executed.")
    echo(f"Watching for: {config.trigger!r}")
    echo(f"Action on trigger: {config.action!r}")
    echo(f"Interval: {config.interval}s | Timeout: {config.timeout}s | Max triggers: {config.max_triggers}")
    echo("Press Ctrl+C to stop.")
    echo("")

    start = time.monotonic()
    trigger_count = 0
    check_count = 0

    while True:
        elapsed = time.monotonic() - start

        if elapsed >= config.timeout:
            result.stopped_reason = "timeout"
            echo(f"Timeout reached ({config.timeout}s). Stopping.")
            break

        if trigger_count >= config.max_triggers:
            result.stopped_reason = "max_triggers"
            echo(f"Reached max_triggers ({config.max_triggers}). Stopping.")
            result.success = trigger_count > 0
            break

        # Capture and check.
        img_bytes = executor.screenshot()
        check_count += 1

        echo(f"  Check #{check_count} ({elapsed:.0f}s elapsed)...", )

        try:
            detected, reasoning = _check_trigger(config, model, img_bytes)
        except Exception as exc:
            echo(f"  Detection error: {exc}")
            time.sleep(config.interval)
            continue

        if detected:
            trigger_count += 1
            echo(f"  TRIGGER DETECTED: {reasoning}")

            event = TriggerEvent(
                index=trigger_count,
                timestamp=time.time(),
                reasoning=reasoning,
            )

            if config.dry_run:
                echo(f"  [DRY RUN] Would execute action: {config.action!r}")
                event.agent_success = False
                event.agent_stopped_reason = "dry_run"
            else:
                echo(f"  Running action: {config.action!r}")
                ok, stopped = _run_action_agent(config, echo, prompt_fn)
                event.agent_success = ok
                event.agent_stopped_reason = stopped

            result.events.append(event)
        else:
            echo(f"  Not detected. ({reasoning[:60]})")

        time.sleep(config.interval)

    result.checks_performed = check_count
    result.triggers_detected = trigger_count
    result.elapsed_seconds = time.monotonic() - start

    if not result.stopped_reason:
        result.stopped_reason = "complete"
    if trigger_count > 0 and not result.error:
        result.success = True
