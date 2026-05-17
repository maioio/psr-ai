"""System prompt builder for the PSR.ai desktop agent.

Produces the structured prompt sent to the vision LLM at each step of the
agent loop. Kept in a separate module so it can be versioned and tested
independently from the loop logic.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from typing import Sequence


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a desktop automation agent. Your job is to diagnose and fix computer
problems by looking at screenshots and taking precise, minimal actions.

Platform: {platform}
Task: {task}
Step: {step}/{max_steps}

RULES:
- Take only ONE action per response.
- Prefer reading before writing. Prefer clicking before typing.
- Use "ask_user" when you need clarification or encounter something unexpected.
- Use "done" when the task is complete OR when you cannot safely proceed.
- Set "is_destructive": true for: file deletion, form submission, sending
  emails, installing/uninstalling software, changing system settings.
- Set "requires_confirmation": true for any action that cannot be undone.
- Never guess coordinates. If you cannot identify the target element clearly
  in the screenshot, use "ask_user" or "done" with an explanation.

HISTORY (last {context_count} steps):
{history}

Respond with ONLY a valid JSON object. No markdown, no explanation outside JSON.
"""

_ACTION_SCHEMA = """\
{
  "reasoning": "<one sentence: what you see and why you chose this action>",
  "action": {
    "type": "<click|double_click|type|key|scroll|move|done|ask_user>",
    "x": <integer, required for click/double_click/scroll/move>,
    "y": <integer, required for click/double_click/scroll/move>,
    "button": "<left|right|middle, optional, default left>",
    "text": "<string, required for type>",
    "key": "<string, required for key, e.g. enter ctrl+c alt+f4>",
    "direction": "<up|down, required for scroll>",
    "amount": <integer, optional scroll units, default 3>,
    "question": "<string, required for ask_user>",
    "result": "<string, required for done - what was accomplished or why stopped>"
  },
  "confidence": <float 0.0-1.0>,
  "is_destructive": <boolean>,
  "requires_confirmation": <boolean>
}"""


# ---------------------------------------------------------------------------
# Screenshot optimization
# ---------------------------------------------------------------------------

_MAX_WIDTH = 1280


def resize_screenshot_bytes(raw: bytes, max_width: int = _MAX_WIDTH) -> bytes:
    """Resize screenshot PNG bytes to at most max_width pixels wide.

    Returns the original bytes unchanged if PIL is unavailable or the image
    is already within the limit. Output format is JPEG when resizing (smaller
    payload); original bytes kept as-is when no resize is needed.
    """
    if not raw:
        return raw
    try:
        from PIL import Image
    except ImportError:
        return raw

    try:
        img = Image.open(io.BytesIO(raw))
        if img.width <= max_width:
            return raw
        ratio = max_width / img.width
        new_height = max(1, int(img.height * ratio))
        img = img.resize((max_width, new_height), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# History formatting
# ---------------------------------------------------------------------------

def _format_history(steps: Sequence[dict], context_count: int) -> str:
    """Return a compact text summary of the last context_count steps."""
    recent = list(steps)[-context_count:]
    if not recent:
        return "(no steps taken yet)"
    lines = []
    for s in recent:
        idx = s.get("index", "?")
        action = s.get("action", {})
        atype = action.get("type", "?")
        reasoning = s.get("claude_reasoning", "")[:120]
        result = s.get("result", "")[:80]
        lines.append(f"  Step {idx}: [{atype}] {reasoning} -> {result}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def build_messages(
    task: str,
    step: int,
    max_steps: int,
    screenshot_b64: str,
    history: Sequence[dict],
    context_count: int = 3,
    screenshot_raw: bytes | None = None,
) -> list[dict]:
    """Build the messages list for one Claude API call.

    Returns a (system_str, messages_list) tuple ready for the Anthropic
    Messages API. The system prompt is passed to client.messages.create()
    'system' parameter; messages to the 'messages' parameter.

    Args:
      task:           the natural-language goal supplied by the user.
      step:           current step index (1-based for display).
      max_steps:      the hard step limit from AgentConfig.
      screenshot_b64: base64-encoded PNG of the current desktop state.
                      Used as fallback when screenshot_raw is None.
      history:        list of AgentStep dicts from previous iterations.
      context_count:  how many history steps to include in the prompt.
      screenshot_raw: raw PNG bytes. When provided, the image is resized to
                      at most 1280px wide before encoding, reducing token cost.
    """
    if screenshot_raw is not None:
        optimized = resize_screenshot_bytes(screenshot_raw)
        img_b64 = base64.b64encode(optimized).decode("ascii")
        # JPEG after resize; PNG when resize was skipped (original unchanged)
        media_type = "image/jpeg" if optimized != screenshot_raw else "image/png"
    else:
        img_b64 = screenshot_b64
        media_type = "image/png"

    system = _SYSTEM.format(
        platform=sys.platform,
        task=task,
        step=step,
        max_steps=max_steps,
        context_count=context_count,
        history=_format_history(history, context_count),
    )

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_b64,
            },
        },
        {
            "type": "text",
            "text": (
                "This is the current state of the desktop. "
                "Respond with a single JSON action following this schema:\n\n"
                + _ACTION_SCHEMA
            ),
        },
    ]

    messages = [{"role": "user", "content": user_content}]
    return system, messages


def parse_response(text: str) -> dict:
    """Parse the LLM text response into a validated action dict.

    Strips markdown fences if present. Raises ValueError on invalid JSON or
    missing required 'action.type' field.
    """
    cleaned = text.strip()
    # Strip ```json ... ``` fences that some models add despite instructions.
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        inner = [
            l for l in lines
            if not l.startswith("```")
        ]
        cleaned = "\n".join(inner).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON: {exc}\nRaw response: {text[:400]}"
        ) from exc

    action = data.get("action", {})
    if not action.get("type"):
        raise ValueError(
            f"LLM response missing action.type. Response: {text[:400]}"
        )

    return data
