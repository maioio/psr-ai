"""Desktop agent loop: capture -> think -> act -> verify.

The agent takes a natural-language task description, repeatedly captures
the screen, asks Claude what to do next, and optionally executes the action.

Safety controls (all ON by default):
  dry_run=True            - show planned actions without executing them.
  ask_before_action=True  - require explicit user approval before every action.
  ask_before_destructive=True - always ask before destructive actions, even
                                when ask_before_action is False.

Typical usage:
    from psr_ai.core.agent import run_agent, AgentConfig
    config = AgentConfig(
        task="The app crashes when I click Save - diagnose and fix it",
        dry_run=False,
        ask_before_action=True,
    )
    result = run_agent(config, echo_fn=print)
    print(result.report())
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """All parameters for a single agent run."""

    task: str
    max_steps: int = 20
    provider: str = "anthropic"
    model: str | None = None
    dry_run: bool = True
    ask_before_action: bool = True
    ask_before_destructive: bool = True
    screenshot_interval: float = 0.5
    context_screenshots: int = 3
    output_dir: Path | None = None
    initial_screenshot: Path | None = None


@dataclass
class AgentStep:
    """Record of one step in the agent loop."""

    index: int
    screenshot_path: Path | None
    claude_reasoning: str
    action: dict
    action_executed: bool
    user_approved: bool | None    # None means no confirmation was needed
    result: str


@dataclass
class AgentResult:
    """Outcome of a complete run_agent() call."""

    task: str
    success: bool
    steps: list[AgentStep] = field(default_factory=list)
    stopped_reason: str = ""
    session_dir: Path | None = None
    error: str = ""

    def report(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"Agent result: {status}",
            f"  Task:    {self.task}",
            f"  Steps:   {len(self.steps)}",
            f"  Stopped: {self.stopped_reason}",
        ]
        if self.session_dir:
            lines.append(f"  Log dir: {self.session_dir}")
        if self.error:
            lines.append(f"  Error:   {self.error}")
        for s in self.steps:
            executed = "executed" if s.action_executed else "skipped"
            atype = s.action.get("type", "?")
            lines.append(f"  [{s.index:02d}] {atype} ({executed}): {s.claude_reasoning[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_agent(
    config: AgentConfig,
    prompt_fn: Callable[[str], str] | None = None,
    echo_fn: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run the desktop agent loop.

    Never raises. All exceptions are caught and written to result.error.

    Args:
      config:    AgentConfig controlling behaviour.
      prompt_fn: callable(message) -> user_input_str. Defaults to input().
                 Inject a custom function in tests to avoid stdin.
      echo_fn:   callable(message) -> None for progress output.
                 Defaults to print().

    Returns AgentResult with full step log.
    """
    _echo = echo_fn or print
    _prompt = prompt_fn or input

    result = AgentResult(task=config.task, success=False)

    try:
        _run(config, result, _echo, _prompt)
    except KeyboardInterrupt:
        result.stopped_reason = "user_interrupt"
        result.error = "Interrupted by user (Ctrl+C)."
        _echo("\nAgent interrupted. Stopping cleanly.")
    except Exception as exc:
        result.error = str(exc)
        result.stopped_reason = "error"
        _echo(f"Agent error: {exc}")

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


def _get_executor():
    """Thin wrapper so tests can patch at 'psr_ai.core.agent._get_executor'."""
    from psr_ai.core.executor import get_executor
    return get_executor()


def _run(
    config: AgentConfig,
    result: AgentResult,
    echo: Callable[[str], None],
    prompt: Callable[[str], str],
) -> None:
    """Internal implementation - raises on unrecoverable errors."""
    from psr_ai.core.agent_prompt import build_messages, parse_response

    # Pre-flight: verify API key before entering the loop.
    key_var = _KEY_VARS.get(config.provider, "")
    if key_var and not os.environ.get(key_var):
        raise ValueError(
            f"{key_var} environment variable is not set. "
            f"Export it before running `sa agent --provider {config.provider}`."
        )

    model = config.model or _DEFAULT_MODELS.get(config.provider, "")

    # Set up output directory for session log.
    if config.output_dir:
        session_dir = Path(config.output_dir)
    else:
        from psr_ai.utils.paths import default_output_dir, timestamp_filename
        base = default_output_dir()
        session_dir = base / timestamp_filename("agent_session", ".d")
    session_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir = session_dir / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)
    result.session_dir = session_dir

    executor = _get_executor()
    history: list[dict] = []

    if config.dry_run:
        echo("DRY RUN mode: actions will be shown but NOT executed.")
    echo(f"Task: {config.task}")
    echo(f"Max steps: {config.max_steps} | Provider: {config.provider} ({model})")
    echo("")

    for step_idx in range(1, config.max_steps + 1):
        echo(f"--- Step {step_idx}/{config.max_steps} ---")

        # Capture screenshot.
        if step_idx == 1 and config.initial_screenshot:
            img_bytes = Path(config.initial_screenshot).read_bytes()
            png_path = screenshots_dir / f"step_{step_idx:03d}.png"
            png_path.write_bytes(img_bytes)
        else:
            if config.screenshot_interval > 0:
                time.sleep(config.screenshot_interval)
            img_bytes = executor.screenshot()
            png_path = screenshots_dir / f"step_{step_idx:03d}.png"
            if img_bytes:
                png_path.write_bytes(img_bytes)

        img_b64 = base64.b64encode(img_bytes).decode("ascii") if img_bytes else ""

        # Ask Claude what to do.
        try:
            response_data = _call_llm(
                config, model, img_b64, history, step_idx,
                build_messages, parse_response,
                screenshot_raw=img_bytes if img_bytes else None,
            )
        except Exception as exc:
            echo(f"  LLM error: {exc}")
            result.stopped_reason = "llm_error"
            result.error = str(exc)
            break

        action = response_data.get("action", {})
        reasoning = response_data.get("reasoning", "")
        confidence = response_data.get("confidence", 0.0)
        is_destructive = response_data.get("is_destructive", False)
        requires_confirmation = response_data.get("requires_confirmation", False)
        atype = action.get("type", "unknown")

        echo(f"  Reasoning: {reasoning}")
        echo(f"  Action: {atype} {_format_action_args(action)} (confidence: {confidence:.0%})")

        # Handle terminal actions.
        if atype == "done":
            echo(f"  Done: {action.get('result', '')}")
            agent_step = AgentStep(
                index=step_idx,
                screenshot_path=png_path if img_bytes else None,
                claude_reasoning=reasoning,
                action=action,
                action_executed=False,
                user_approved=None,
                result=action.get("result", "task complete"),
            )
            result.steps.append(agent_step)
            history.append(_step_to_history(agent_step))
            result.success = True
            result.stopped_reason = "task_complete"
            break

        if atype == "ask_user":
            question = action.get("question", "What should I do?")
            echo(f"  Question: {question}")
            user_answer = prompt(f"  Your answer: ")
            agent_step = AgentStep(
                index=step_idx,
                screenshot_path=png_path if img_bytes else None,
                claude_reasoning=reasoning,
                action=action,
                action_executed=False,
                user_approved=True,
                result=f"user answered: {user_answer}",
            )
            result.steps.append(agent_step)
            history.append(_step_to_history(agent_step))
            # Feed the answer back into history for next iteration.
            history[-1]["user_answer"] = user_answer
            continue

        # Determine if we need explicit approval.
        needs_approval = (
            config.dry_run
            or (config.ask_before_action)
            or (is_destructive and config.ask_before_destructive)
            or requires_confirmation
        )

        approved = False
        if needs_approval:
            if config.dry_run:
                echo("  [DRY RUN] Would execute the action above. Skipping.")
                approved = False
            else:
                answer = prompt(
                    f"  Execute this action? [y/N]: "
                ).strip().lower()
                approved = answer in ("y", "yes")
                if not approved:
                    echo("  Skipped by user.")
        else:
            approved = True

        # Execute.
        action_executed = False
        exec_result = "skipped"
        if approved and not config.dry_run:
            try:
                executor.dispatch(action)
                action_executed = True
                exec_result = "executed"
                echo(f"  Executed.")
            except Exception as exc:
                exec_result = f"failed: {exc}"
                echo(f"  Execution error: {exc}")

        agent_step = AgentStep(
            index=step_idx,
            screenshot_path=png_path if img_bytes else None,
            claude_reasoning=reasoning,
            action=action,
            action_executed=action_executed,
            user_approved=approved if needs_approval else None,
            result=exec_result,
        )
        result.steps.append(agent_step)
        history.append(_step_to_history(agent_step))

    else:
        # Loop exhausted without "done".
        result.stopped_reason = "max_steps"
        echo(f"Reached max_steps ({config.max_steps}). Stopping.")

    # Write session log.
    _write_session_log(result, session_dir)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(
    config: AgentConfig,
    model: str,
    img_b64: str,
    history: list[dict],
    step_idx: int,
    build_messages: Callable,
    parse_response: Callable,
    screenshot_raw: bytes | None = None,
) -> dict:
    """Call the vision LLM and return the parsed response dict."""
    system, messages = build_messages(
        task=config.task,
        step=step_idx,
        max_steps=config.max_steps,
        screenshot_b64=img_b64,
        history=history,
        context_count=config.context_screenshots,
        screenshot_raw=screenshot_raw,
    )

    if config.provider == "anthropic":
        return _call_anthropic(model, system, messages, parse_response)
    if config.provider == "openai":
        return _call_openai(model, system, messages, parse_response)
    raise ValueError(f"Unknown provider: {config.provider!r}")


def _call_anthropic(model: str, system: str, messages: list, parse_response: Callable) -> dict:
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package required. Install with: pip install psr-ai[agent]"
        ) from exc

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    return parse_response(response.content[0].text)


def _call_openai(model: str, system: str, messages: list, parse_response: Callable) -> dict:
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package required. Install with: pip install psr-ai[ai]"
        ) from exc

    # Prepend system message in OpenAI format.
    full_messages = [{"role": "system", "content": system}] + messages
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=1024,
        messages=full_messages,
    )
    return parse_response(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_action_args(action: dict) -> str:
    """Return a short human-readable summary of an action's parameters."""
    atype = action.get("type", "")
    if atype in ("click", "double_click", "move"):
        return f"({action.get('x', '?')}, {action.get('y', '?')})"
    if atype == "type":
        text = action.get("text", "")
        return f"{text[:40]!r}{'...' if len(text) > 40 else ''}"
    if atype == "key":
        return action.get("key", "")
    if atype == "scroll":
        return f"({action.get('x', '?')}, {action.get('y', '?')}) {action.get('direction', '?')}"
    if atype == "ask_user":
        return action.get("question", "")[:60]
    if atype == "done":
        return action.get("result", "")[:60]
    return ""


def _step_to_history(step: AgentStep) -> dict:
    """Convert an AgentStep to a compact dict for the history context."""
    return {
        "index": step.index,
        "action": step.action,
        "claude_reasoning": step.claude_reasoning,
        "result": step.result,
    }


def _write_session_log(result: AgentResult, session_dir: Path) -> None:
    """Write a data.json session log compatible with PSR.ai's standard schema."""
    steps_data = []
    for s in result.steps:
        atype = s.action.get("type", "")
        x = s.action.get("x", 0)
        y = s.action.get("y", 0)
        steps_data.append({
            "index": s.index,
            "label": f"Step {s.index}: {atype}",
            "timestamp": "",
            "ui_element": "",
            "program": "",
            "action_text": s.claude_reasoning,
            "screenshot": (
                f"screenshots/step_{s.index:03d}.png"
                if s.screenshot_path and s.screenshot_path.exists()
                else ""
            ),
            "click_x": x,
            "click_y": y,
            "agent_action": s.action,
            "action_executed": s.action_executed,
            "result": s.result,
        })

    payload = {
        "title": f"Agent Session: {result.task[:80]}",
        "source": "agent_loop",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "step_count": len(result.steps),
        "success": result.success,
        "stopped_reason": result.stopped_reason,
        "steps": steps_data,
    }
    log_path = session_dir / "data.json"
    log_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
