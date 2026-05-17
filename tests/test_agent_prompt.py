"""Unit tests for core/agent_prompt.py - prompt builder and screenshot resize."""

from __future__ import annotations

import base64
import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from psr_ai.core.agent_prompt import (
    build_messages,
    parse_response,
    resize_screenshot_bytes,
)


# ---------------------------------------------------------------------------
# Minimal valid PNG bytes (1x1 pixel)
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_wide_png(width: int = 1600, height: int = 900) -> bytes:
    """Create a minimal PNG wider than 1280px using PIL."""
    try:
        from PIL import Image
        img = Image.new("RGB", (width, height), color=(128, 64, 32))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        pytest.skip("PIL not installed")


# ---------------------------------------------------------------------------
# resize_screenshot_bytes
# ---------------------------------------------------------------------------

class TestResizeScreenshotBytes:

    def test_returns_original_when_empty(self):
        assert resize_screenshot_bytes(b"") == b""

    def test_returns_original_when_pil_unavailable(self):
        with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
            result = resize_screenshot_bytes(_TINY_PNG)
        assert result == _TINY_PNG

    def test_small_image_not_resized(self):
        result = resize_screenshot_bytes(_TINY_PNG, max_width=1280)
        assert result == _TINY_PNG

    def test_wide_image_is_resized(self):
        wide = _make_wide_png(1600, 900)
        result = resize_screenshot_bytes(wide, max_width=1280)
        assert len(result) < len(wide)

    def test_wide_image_result_width_at_most_max(self):
        from PIL import Image
        wide = _make_wide_png(1600, 900)
        result = resize_screenshot_bytes(wide, max_width=1280)
        img = Image.open(io.BytesIO(result))
        assert img.width <= 1280

    def test_aspect_ratio_preserved(self):
        from PIL import Image
        wide = _make_wide_png(1600, 800)
        result = resize_screenshot_bytes(wide, max_width=1280)
        img = Image.open(io.BytesIO(result))
        expected_height = int(800 * (1280 / 1600))
        assert abs(img.height - expected_height) <= 1

    def test_returns_original_on_corrupt_bytes(self):
        corrupt = b"\x89PNG corrupt data that cannot be parsed"
        result = resize_screenshot_bytes(corrupt, max_width=1280)
        assert result == corrupt


# ---------------------------------------------------------------------------
# build_messages - screenshot_raw path (Phase 10 optimization)
# ---------------------------------------------------------------------------

class TestBuildMessagesWithRaw:

    def test_screenshot_raw_overrides_b64_when_small(self):
        """When raw bytes fit within max_width, original PNG is used as-is."""
        _, messages = build_messages(
            task="test",
            step=1,
            max_steps=5,
            screenshot_b64="old_b64",
            history=[],
            context_count=3,
            screenshot_raw=_TINY_PNG,
        )
        img_data = messages[0]["content"][0]["source"]["data"]
        expected = base64.b64encode(_TINY_PNG).decode("ascii")
        assert img_data == expected

    def test_screenshot_raw_none_falls_back_to_b64(self):
        fake_b64 = base64.b64encode(b"fake").decode("ascii")
        _, messages = build_messages(
            task="test",
            step=1,
            max_steps=5,
            screenshot_b64=fake_b64,
            history=[],
            context_count=3,
            screenshot_raw=None,
        )
        img_data = messages[0]["content"][0]["source"]["data"]
        assert img_data == fake_b64

    def test_wide_raw_produces_jpeg_media_type(self):
        wide = _make_wide_png(1600, 900)
        _, messages = build_messages(
            task="test",
            step=1,
            max_steps=5,
            screenshot_b64="",
            history=[],
            screenshot_raw=wide,
        )
        media_type = messages[0]["content"][0]["source"]["media_type"]
        assert media_type == "image/jpeg"

    def test_small_raw_keeps_png_media_type(self):
        _, messages = build_messages(
            task="test",
            step=1,
            max_steps=5,
            screenshot_b64="",
            history=[],
            screenshot_raw=_TINY_PNG,
        )
        media_type = messages[0]["content"][0]["source"]["media_type"]
        assert media_type == "image/png"

    def test_messages_structure_correct(self):
        _, messages = build_messages(
            task="open calc",
            step=2,
            max_steps=10,
            screenshot_b64="",
            history=[],
            screenshot_raw=_TINY_PNG,
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        content = messages[0]["content"]
        assert content[0]["type"] == "image"
        assert content[1]["type"] == "text"

    def test_system_prompt_contains_task(self):
        system, _ = build_messages(
            task="unique task xyz",
            step=1,
            max_steps=5,
            screenshot_b64="",
            history=[],
        )
        assert "unique task xyz" in system

    def test_system_prompt_contains_step(self):
        system, _ = build_messages(
            task="t",
            step=3,
            max_steps=10,
            screenshot_b64="",
            history=[],
        )
        assert "3/10" in system


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:

    def test_valid_json_parsed(self):
        raw = '{"reasoning": "ok", "action": {"type": "done", "result": "x"}, "confidence": 1.0, "is_destructive": false, "requires_confirmation": false}'
        data = parse_response(raw)
        assert data["action"]["type"] == "done"

    def test_markdown_fence_stripped(self):
        raw = '```json\n{"reasoning": "r", "action": {"type": "done", "result": "x"}, "confidence": 0.9, "is_destructive": false, "requires_confirmation": false}\n```'
        data = parse_response(raw)
        assert data["action"]["type"] == "done"

    def test_missing_action_type_raises(self):
        raw = '{"reasoning": "r", "action": {}, "confidence": 0.5}'
        with pytest.raises(ValueError, match="action.type"):
            parse_response(raw)

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_response("not json at all {{{")
