"""AI analysis engine: sends session screenshots to a vision LLM and enriches data.json."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Image format detection
# ---------------------------------------------------------------------------

def _detect_image_media_type(data: bytes) -> str:
    """Return the true MIME type of image bytes by inspecting magic bytes.

    Supports JPEG and PNG. Falls back to image/png for unrecognised formats so
    the existing behaviour is preserved for edge cases.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/png"


def _b64_to_bytes(image_b64: str) -> bytes:
    return base64.b64decode(image_b64)


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

def _analyze_with_anthropic(image_b64: str, model: str) -> str:
    """Send one base64-encoded image to Anthropic and return a one-sentence description."""
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "anthropic package required. Install with: pip install psr-ai[ai]"
        ) from exc

    media_type = _detect_image_media_type(_b64_to_bytes(image_b64))

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe what the user is doing in this screenshot "
                            "in one concise sentence. Focus on the action and the "
                            "application visible. Be direct and specific."
                        ),
                    },
                ],
            }
        ],
    )
    return message.content[0].text.strip()


def _analyze_with_openai(image_b64: str, model: str) -> str:
    """Send one base64-encoded image to OpenAI and return a one-sentence description."""
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package required. Install with: pip install psr-ai[ai]"
        ) from exc

    media_type = _detect_image_media_type(_b64_to_bytes(image_b64))

    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe what the user is doing in this screenshot "
                            "in one concise sentence. Focus on the action and the "
                            "application visible. Be direct and specific."
                        ),
                    },
                ],
            }
        ],
    )
    return response.choices[0].message.content.strip()


_PROVIDERS: dict[str, tuple[Callable, str]] = {
    "anthropic": (_analyze_with_anthropic, "claude-sonnet-4-6"),
    "openai":    (_analyze_with_openai,    "gpt-4o"),
}

_DEFAULT_MODELS = {k: v[1] for k, v in _PROVIDERS.items()}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_session(
    source_dir: Path,
    *,
    provider: str = "anthropic",
    model: str | None = None,
    regenerate: bool = True,
    echo_fn: Callable[[str], None] | None = None,
) -> int:
    """Send each screenshot to a vision LLM and write ai_description back to data.json.

    Reads data.json and screenshots/ from source_dir. For every step that has a
    screenshot on disk, calls the chosen vision model and writes the returned
    description into step["ai_description"]. Steps without a screenshot are skipped.

    After enrichment, optionally regenerates report.md so the new descriptions
    appear in the timeline.

    Parameters:
      source_dir - session output directory containing data.json and screenshots/.
      provider   - "anthropic" (default) or "openai".
      model      - override the provider's default model.
      regenerate - when True, re-render report.md after enrichment.
      echo_fn    - progress output function; defaults to print().

    Returns the number of steps that were successfully enriched.
    """
    source_dir = Path(source_dir)
    data_path = source_dir / "data.json"
    screenshots_dir = source_dir / "screenshots"

    if not data_path.exists():
        raise FileNotFoundError(
            f"data.json not found in {source_dir}. "
            "Run `sa stop` first to generate session data."
        )

    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider}'. Choose from: {', '.join(_PROVIDERS)}"
        )

    # Verify the required API key is present before sending any network requests.
    # This surfaces a clear error immediately rather than per-step inside the loop.
    import os as _os
    _KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    _key_var = _KEY_VARS.get(provider, "")
    if _key_var and not _os.environ.get(_key_var):
        raise ValueError(
            f"{_key_var} environment variable is not set. "
            f"Export it before running `sa analyze --provider {provider}`."
        )

    _echo = echo_fn if echo_fn is not None else print
    analyze_fn, default_model = _PROVIDERS[provider]
    resolved_model = model or default_model

    data = json.loads(data_path.read_text(encoding="utf-8"))
    steps: list[dict] = data.get("steps", [])

    if not steps:
        _echo("No steps found in data.json. Nothing to analyze.")
        return 0

    enriched = 0
    total = len(steps)

    _echo(f"\nAnalyzing {total} steps with {provider} ({resolved_model})...")
    _echo("Steps without screenshots will be skipped.\n")

    for step in steps:
        index = step.get("index", "?")
        screenshot_rel = step.get("screenshot", "")
        if not screenshot_rel:
            _echo(f"  Step {index}: skipped (no screenshot)")
            continue

        png_path = screenshots_dir / Path(screenshot_rel).name
        if not png_path.exists():
            _echo(f"  Step {index}: skipped (file not found: {png_path.name})")
            continue

        try:
            image_b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
            description = analyze_fn(image_b64, resolved_model)
            step["ai_description"] = description
            enriched += 1
            _echo(f"  Step {index}: {description[:80]}{'...' if len(description) > 80 else ''}")
        except Exception as exc:
            _echo(f"  Step {index}: error - {exc}")

    # Rewrite data.json with the enriched steps.
    data_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _echo(f"\nEnriched {enriched}/{total} steps. data.json updated.")

    if regenerate and enriched > 0:
        _regenerate(source_dir, data_path, _echo)

    return enriched


def _regenerate(source_dir: Path, data_path: Path, echo_fn: Callable) -> None:
    """Re-render report.md so ai_description fields appear in the timeline."""
    try:
        from psr_ai.core.generator import MarkdownGenerator
        report_path = MarkdownGenerator(data_path).write(source_dir)
        echo_fn(f"Report regenerated: {report_path}")
    except Exception as exc:
        echo_fn(f"Warning: could not regenerate report ({exc}).")
