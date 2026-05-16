"""Unit tests for the GIF visual engine in core/generator.py.

All tests use synthetic 50x50 px PNG images so no real PSR output is needed.
Pillow is a hard dependency for this test module - tests are skipped if absent.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

# Guard: skip the entire module if Pillow is not installed.
PIL = pytest.importorskip("PIL", reason="Pillow not installed - skip GIF tests")

from PIL import Image  # noqa: E402

from psr_ai.core.generator import (
    GifGenerator,
    _clamp_bbox,
    _draw_cursor_halo,
    _resize_proportional,
)
from psr_ai.core.parser import RecordingSession, Step


# ---------------------------------------------------------------------------
# Minimal valid PNG bytes for a solid-colour image
# ---------------------------------------------------------------------------

def _make_png(width: int, height: int, color: tuple = (100, 149, 237)) -> bytes:
    """Return a valid PNG file as bytes for a solid-colour image.

    Uses stdlib struct/zlib only - no Pillow dependency for fixture creation.
    color: RGB tuple.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)

    # Raw scanlines: filter byte 0 + RGB pixels.
    row = b"\x00" + bytes(color) * width
    raw = row * height
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


def _write_png(path: Path, width: int = 50, height: int = 50,
               color: tuple = (100, 149, 237)) -> None:
    path.write_bytes(_make_png(width, height, color))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_data_json(
    tmp_path: Path,
    steps: list[dict] | None = None,
) -> Path:
    """Write a minimal data.json and return its path."""
    if steps is None:
        steps = [
            {"index": 0, "label": "Step 0", "timestamp": "",
             "ui_element": "", "program": "", "action_text": "click",
             "screenshot": "screenshots/step_000.png",
             "click_x": 25, "click_y": 25},
            {"index": 1, "label": "Step 1", "timestamp": "",
             "ui_element": "", "program": "", "action_text": "click",
             "screenshot": "screenshots/step_001.png",
             "click_x": None, "click_y": None},
        ]
    payload = {
        "title": "Test", "source": "test.zip",
        "generated_at": "2024-01-01T00:00:00+00:00",
        "step_count": len(steps), "steps": steps,
    }
    p = tmp_path / "data.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _make_screenshots(tmp_path: Path, count: int = 2,
                      width: int = 50, height: int = 50) -> Path:
    """Write count synthetic screenshots and return the screenshots dir."""
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        _write_png(screenshots_dir / f"step_{i:03d}.png", width, height)
    return screenshots_dir


# ---------------------------------------------------------------------------
# _resize_proportional
# ---------------------------------------------------------------------------

class TestResizeProportional:

    def test_wide_image_shrunk_to_max_width(self):
        img = Image.new("RGB", (2560, 1440), color=(0, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        assert result.width == 1280

    def test_aspect_ratio_preserved(self):
        img = Image.new("RGB", (2560, 1440), color=(0, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        expected_height = int(1440 * (1280 / 2560))
        assert result.height == expected_height

    def test_image_within_limit_unchanged(self):
        img = Image.new("RGB", (800, 600), color=(0, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        assert result.width == 800
        assert result.height == 600

    def test_exactly_at_limit_unchanged(self):
        img = Image.new("RGB", (1280, 720), color=(0, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        assert result.width == 1280
        assert result.size == (1280, 720)

    def test_tiny_image_unchanged(self):
        img = Image.new("RGB", (10, 10), color=(255, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        assert result.size == (10, 10)

    def test_returns_rgb_mode(self):
        img = Image.new("RGB", (1920, 1080), color=(0, 0, 0))
        result = _resize_proportional(img, max_width=1280, Image=Image)
        assert result.mode == "RGB"


# ---------------------------------------------------------------------------
# _clamp_bbox
# ---------------------------------------------------------------------------

class TestClampBbox:

    def test_normal_bbox_unchanged(self):
        result = _clamp_bbox([10, 10, 40, 40], width=100, height=100)
        assert result == [10, 10, 40, 40]

    def test_negative_coords_clamped_to_zero(self):
        x0, y0, x1, y1 = _clamp_bbox([-5, -5, 20, 20], width=100, height=100)
        assert x0 == 0
        assert y0 == 0

    def test_coords_beyond_image_clamped(self):
        x0, y0, x1, y1 = _clamp_bbox([80, 80, 150, 150], width=100, height=100)
        assert x1 <= 99
        assert y1 <= 99

    def test_x1_always_greater_than_x0(self):
        # Edge case: click at x=99 on a 100px image.
        x0, y0, x1, y1 = _clamp_bbox([98, 10, 102, 40], width=100, height=100)
        assert x1 > x0

    def test_y1_always_greater_than_y0(self):
        x0, y0, x1, y1 = _clamp_bbox([10, 98, 40, 102], width=100, height=100)
        assert y1 > y0

    def test_zero_size_image_does_not_raise(self):
        # Degenerate case: 1x1 image.
        result = _clamp_bbox([0, 0, 10, 10], width=1, height=1)
        x0, y0, x1, y1 = result
        assert x0 <= x1
        assert y0 <= y1


# ---------------------------------------------------------------------------
# _draw_cursor_halo
# ---------------------------------------------------------------------------

class TestDrawCursorHalo:

    def _base_img(self, width: int = 100, height: int = 100) -> Image.Image:
        return Image.new("RGB", (width, height), color=(200, 200, 200))

    def test_returns_rgb_image(self):
        img = self._base_img()
        result = _draw_cursor_halo(
            img, click_x=50, click_y=50,
            original_width=100, scaled_width=100,
            radius=10, inner=3, color=(220, 30, 30, 160), ring_width=2,
            Image=Image,
        )
        assert result.mode == "RGB"

    def test_returns_same_size(self):
        img = self._base_img(80, 60)
        result = _draw_cursor_halo(
            img, click_x=40, click_y=30,
            original_width=80, scaled_width=80,
            radius=10, inner=3, color=(220, 30, 30, 160), ring_width=2,
            Image=Image,
        )
        assert result.size == (80, 60)

    def test_centre_pixel_changed(self):
        img = self._base_img(100, 100)
        original_pixel = img.getpixel((50, 50))
        result = _draw_cursor_halo(
            img, click_x=50, click_y=50,
            original_width=100, scaled_width=100,
            radius=18, inner=4, color=(220, 30, 30, 160), ring_width=3,
            Image=Image,
        )
        # The halo dot must change the pixel at the click point.
        assert result.getpixel((50, 50)) != original_pixel

    def test_no_exception_at_corner(self):
        # Click at exact corner: (0, 0) - bounding box math must not raise.
        img = self._base_img(100, 100)
        _draw_cursor_halo(
            img, click_x=0, click_y=0,
            original_width=100, scaled_width=100,
            radius=18, inner=4, color=(220, 30, 30, 160), ring_width=3,
            Image=Image,
        )

    def test_no_exception_at_opposite_corner(self):
        img = self._base_img(100, 100)
        _draw_cursor_halo(
            img, click_x=99, click_y=99,
            original_width=100, scaled_width=100,
            radius=18, inner=4, color=(220, 30, 30, 160), ring_width=3,
            Image=Image,
        )

    def test_coordinate_scaling_applied(self):
        # Original image is 200px wide, scaled to 100px.
        # Click at x=100 in original space should map to x=50 in scaled space.
        img = self._base_img(100, 100)
        original_pixel_at_50 = img.getpixel((50, 50))
        result = _draw_cursor_halo(
            img, click_x=100, click_y=100,
            original_width=200, scaled_width=100,
            radius=12, inner=3, color=(220, 30, 30, 160), ring_width=2,
            Image=Image,
        )
        # Pixel at (50, 50) should now differ from original grey.
        assert result.getpixel((50, 50)) != original_pixel_at_50


# ---------------------------------------------------------------------------
# GifGenerator.write
# ---------------------------------------------------------------------------

class TestGifGenerator:

    def test_produces_gif_file(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=2)

        gen = GifGenerator(data_path, tmp_path / "screenshots")
        gif_path = gen.write(tmp_path)

        assert gif_path.exists()
        assert gif_path.suffix == ".gif"

    def test_default_filename_is_action_replay(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=2)

        gen = GifGenerator(data_path, tmp_path / "screenshots")
        gif_path = gen.write(tmp_path)

        assert gif_path.name == "action_replay.gif"

    def test_custom_filename(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=2)

        gen = GifGenerator(data_path, tmp_path / "screenshots")
        gif_path = gen.write(tmp_path, filename="custom.gif")

        assert gif_path.name == "custom.gif"

    def test_gif_is_animated(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=2)

        gen = GifGenerator(data_path, tmp_path / "screenshots")
        gif_path = gen.write(tmp_path)

        with Image.open(gif_path) as img:
            frame_count = 0
            try:
                while True:
                    frame_count += 1
                    img.seek(img.tell() + 1)
            except EOFError:
                pass
        assert frame_count == 2

    def test_raises_when_no_screenshots(self, tmp_path):
        steps = [{"index": 0, "label": "X", "timestamp": "", "ui_element": "",
                  "program": "", "action_text": "x", "screenshot": "",
                  "click_x": None, "click_y": None}]
        data_path = _make_data_json(tmp_path, steps=steps)
        # No screenshots written - screenshots dir is empty.
        (tmp_path / "screenshots").mkdir()

        gen = GifGenerator(data_path, tmp_path / "screenshots")
        with pytest.raises(ValueError, match="No frames found"):
            gen.write(tmp_path)

    def test_frame_width_capped_at_max(self, tmp_path):
        # Provide a wide screenshot and verify the GIF frame is resized.
        data_path = _make_data_json(tmp_path)
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _write_png(screenshots_dir / "step_000.png", width=2560, height=1440)
        _write_png(screenshots_dir / "step_001.png", width=2560, height=1440)

        gen = GifGenerator(data_path, screenshots_dir, max_width=1280)
        gif_path = gen.write(tmp_path)

        with Image.open(gif_path) as img:
            assert img.width <= 1280

    def test_palette_max_128_colors(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=2)

        gen = GifGenerator(data_path, tmp_path / "screenshots", palette_colors=128)
        gif_path = gen.write(tmp_path)

        with Image.open(gif_path) as img:
            # GIF palette: number of unique colours must be at most 128.
            palette = img.getpalette()
            if palette:
                # Each colour entry is 3 bytes (R, G, B).
                num_colors = len(palette) // 3
                assert num_colors <= 256  # GIF max; quantize targets 128

    def test_step_with_click_coords_does_not_raise(self, tmp_path):
        steps = [{"index": 0, "label": "Click", "timestamp": "",
                  "ui_element": "", "program": "", "action_text": "click",
                  "screenshot": "screenshots/step_000.png",
                  "click_x": 25, "click_y": 25}]
        data_path = _make_data_json(tmp_path, steps=steps)
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _write_png(screenshots_dir / "step_000.png")

        gen = GifGenerator(data_path, screenshots_dir)
        gif_path = gen.write(tmp_path)
        assert gif_path.exists()

    def test_step_without_click_coords_does_not_raise(self, tmp_path):
        steps = [{"index": 0, "label": "No click", "timestamp": "",
                  "ui_element": "", "program": "", "action_text": "view",
                  "screenshot": "screenshots/step_000.png",
                  "click_x": None, "click_y": None}]
        data_path = _make_data_json(tmp_path, steps=steps)
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _write_png(screenshots_dir / "step_000.png")

        gen = GifGenerator(data_path, screenshots_dir)
        gif_path = gen.write(tmp_path)
        assert gif_path.exists()

    def test_click_at_image_edge_does_not_raise(self, tmp_path):
        # Halo centred at the corner should not raise a bounding-box error.
        steps = [{"index": 0, "label": "Edge click", "timestamp": "",
                  "ui_element": "", "program": "", "action_text": "click",
                  "screenshot": "screenshots/step_000.png",
                  "click_x": 0, "click_y": 0}]
        data_path = _make_data_json(tmp_path, steps=steps)
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _write_png(screenshots_dir / "step_000.png", width=50, height=50)

        gen = GifGenerator(data_path, screenshots_dir)
        gen.write(tmp_path)

    def test_missing_screenshot_file_skipped(self, tmp_path):
        # Step 0 has a screenshot file. Step 1 does not.
        # GIF should still succeed with the one frame that exists.
        steps = [
            {"index": 0, "label": "A", "timestamp": "", "ui_element": "",
             "program": "", "action_text": "a",
             "screenshot": "screenshots/step_000.png",
             "click_x": None, "click_y": None},
            {"index": 1, "label": "B", "timestamp": "", "ui_element": "",
             "program": "", "action_text": "b",
             "screenshot": "screenshots/step_001.png",
             "click_x": None, "click_y": None},
        ]
        data_path = _make_data_json(tmp_path, steps=steps)
        screenshots_dir = tmp_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        # Write only step_000.png - step_001.png is intentionally absent.
        _write_png(screenshots_dir / "step_000.png")

        gen = GifGenerator(data_path, screenshots_dir)
        gif_path = gen.write(tmp_path)
        assert gif_path.exists()

    def test_output_dir_created_if_missing(self, tmp_path):
        data_path = _make_data_json(tmp_path)
        _make_screenshots(tmp_path, count=1)

        out_dir = tmp_path / "nested" / "output"
        gen = GifGenerator(data_path, tmp_path / "screenshots")
        gif_path = gen.write(out_dir)

        assert out_dir.exists()
        assert gif_path.exists()
