"""Unit tests for core/analyzer.py - AI description enrichment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.analyzer import analyze_session, _PROVIDERS, _DEFAULT_MODELS


# Provide a fake API key for all tests so the pre-check in analyze_session does
# not raise before the mocked provider function is ever called.
@pytest.fixture(autouse=True)
def _fake_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-openai")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_session_dir(tmp_path: Path, steps: list[dict]) -> tuple[Path, Path]:
    """Write data.json and dummy PNGs; return (source_dir, data_path)."""
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()

    # Write a 1x1 transparent PNG for steps that have screenshots.
    _TINY_PNG = bytes([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41,
        0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
        0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc,
        0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
        0x44, 0xae, 0x42, 0x60, 0x82,
    ])

    for step in steps:
        ss = step.get("screenshot", "")
        if ss:
            png_name = Path(ss).name
            (screenshots_dir / png_name).write_bytes(_TINY_PNG)

    data = {
        "title": "Test Session",
        "source": "test.zip",
        "generated_at": "2024-01-15T14:30:00Z",
        "step_count": len(steps),
        "steps": steps,
    }
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    return tmp_path, data_path


def _make_steps(n: int, with_screenshots: bool = True) -> list[dict]:
    return [
        {
            "index": i,
            "label": f"Step {i}",
            "timestamp": "",
            "program": "TestApp",
            "action_text": f"Action {i}",
            "screenshot": f"screenshots/step_{i:03d}.png" if with_screenshots else "",
        }
        for i in range(n)
    ]


# ------------------------------------------------------------------
# Provider dispatch
# ------------------------------------------------------------------

def test_providers_dict_contains_expected_keys():
    assert "anthropic" in _PROVIDERS
    assert "openai" in _PROVIDERS


def test_default_models_are_set():
    assert _DEFAULT_MODELS["anthropic"] == "claude-sonnet-4-6"
    assert _DEFAULT_MODELS["openai"] == "gpt-4o"


# ------------------------------------------------------------------
# analyze_session - basic enrichment
# ------------------------------------------------------------------

def test_analyze_session_writes_ai_description(tmp_path):
    steps = _make_steps(3)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    mock_fn = MagicMock(return_value="The user clicked the button.")
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "test-model")}):
        enriched = analyze_session(source_dir, provider="anthropic", regenerate=False)

    assert enriched == 3
    data = json.loads((source_dir / "data.json").read_text(encoding="utf-8"))
    for step in data["steps"]:
        assert step["ai_description"] == "The user clicked the button."


def test_analyze_session_skips_steps_without_screenshots(tmp_path):
    steps = _make_steps(3, with_screenshots=False)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    mock_fn = MagicMock(return_value="Should not be called.")
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "test-model")}):
        enriched = analyze_session(source_dir, provider="anthropic", regenerate=False)

    assert enriched == 0
    mock_fn.assert_not_called()


def test_analyze_session_skips_missing_png_files(tmp_path):
    steps = _make_steps(2)
    source_dir, data_path = _make_session_dir(tmp_path, steps)

    # Delete one PNG to simulate a missing file.
    (source_dir / "screenshots" / "step_001.png").unlink()

    mock_fn = MagicMock(return_value="Description.")
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "test-model")}):
        enriched = analyze_session(source_dir, provider="anthropic", regenerate=False)

    assert enriched == 1  # Only step_000 was processed.


def test_analyze_session_continues_after_per_step_error(tmp_path):
    steps = _make_steps(3)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    call_count = 0

    def flaky_fn(image_b64, model):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("API timeout")
        return "OK description"

    messages = []
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (flaky_fn, "test-model")}):
        enriched = analyze_session(
            source_dir,
            provider="anthropic",
            regenerate=False,
            echo_fn=messages.append,
        )

    assert enriched == 2  # Step 1 failed; steps 0 and 2 succeeded.
    assert any("error" in m.lower() for m in messages)


def test_analyze_session_raises_on_missing_data_json(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyze_session(tmp_path, provider="anthropic", regenerate=False)


def test_analyze_session_raises_on_unknown_provider(tmp_path):
    steps = _make_steps(1)
    source_dir, _ = _make_session_dir(tmp_path, steps)
    with pytest.raises(ValueError, match="Unknown provider"):
        analyze_session(source_dir, provider="imaginary_ai", regenerate=False)


# ------------------------------------------------------------------
# Model override
# ------------------------------------------------------------------

def test_analyze_session_passes_custom_model(tmp_path):
    steps = _make_steps(1)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    received_models = []

    def capture_fn(image_b64, model):
        received_models.append(model)
        return "desc"

    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (capture_fn, "default-model")}):
        analyze_session(source_dir, provider="anthropic", model="custom-model-v1", regenerate=False)

    assert received_models == ["custom-model-v1"]


def test_analyze_session_uses_provider_default_model_when_none(tmp_path):
    steps = _make_steps(1)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    received_models = []

    def capture_fn(image_b64, model):
        received_models.append(model)
        return "desc"

    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (capture_fn, "provider-default")}):
        analyze_session(source_dir, provider="anthropic", model=None, regenerate=False)

    assert received_models == ["provider-default"]


# ------------------------------------------------------------------
# Regeneration
# ------------------------------------------------------------------

def test_analyze_session_triggers_report_regen_by_default(tmp_path):
    steps = _make_steps(1)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    mock_fn = MagicMock(return_value="desc")
    with (
        patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "m")}),
        patch("psr_ai.core.generator.MarkdownGenerator") as mock_gen,
    ):
        mock_gen.return_value.write.return_value = source_dir / "report.md"
        analyze_session(source_dir, provider="anthropic")

    mock_gen.assert_called_once()


def test_analyze_session_skips_regen_when_flag_set(tmp_path):
    steps = _make_steps(1)
    source_dir, _ = _make_session_dir(tmp_path, steps)

    mock_fn = MagicMock(return_value="desc")
    with (
        patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "m")}),
        patch("psr_ai.core.generator.MarkdownGenerator") as mock_gen,
    ):
        analyze_session(source_dir, provider="anthropic", regenerate=False)

    mock_gen.assert_not_called()


def test_analyze_session_returns_zero_on_empty_steps(tmp_path):
    source_dir, _ = _make_session_dir(tmp_path, [])
    mock_fn = MagicMock(return_value="desc")
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "m")}):
        result = analyze_session(source_dir, provider="anthropic", regenerate=False)
    assert result == 0


# ------------------------------------------------------------------
# data.json integrity after enrichment
# ------------------------------------------------------------------

def test_non_screenshot_steps_are_preserved_in_data_json(tmp_path):
    steps = [
        {"index": 0, "label": "A", "timestamp": "", "program": "P", "action_text": "act", "screenshot": "screenshots/step_000.png"},
        {"index": 1, "label": "B", "timestamp": "", "program": "P", "action_text": "act", "screenshot": ""},
    ]
    source_dir, _ = _make_session_dir(tmp_path, steps)

    mock_fn = MagicMock(return_value="desc")
    with patch.dict("psr_ai.core.analyzer._PROVIDERS", {"anthropic": (mock_fn, "m")}):
        analyze_session(source_dir, provider="anthropic", regenerate=False)

    data = json.loads((source_dir / "data.json").read_text(encoding="utf-8"))
    assert len(data["steps"]) == 2  # Both steps survive.
    assert data["steps"][0]["ai_description"] == "desc"
    assert "ai_description" not in data["steps"][1]  # No screenshot = no enrichment.
