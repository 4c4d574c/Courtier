"""Tests for image-based font size refinement."""

import tempfile
from pathlib import Path

import numpy as np
from docparse.parsers.font_image import (
    _binarize,
    _char_fg_height,
    refine_font_sizes_from_image,
)
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Synthetic character image helpers
# ---------------------------------------------------------------------------


def _make_char_image(
    width: int = 48,
    height: int = 48,
    stroke_width: int = 3,
) -> np.ndarray:
    """Draw a synthetic CJK-like character as a grayscale numpy array.

    Draws a grid-like pattern on a white background.
    """
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)

    h_center = height // 2
    w_center = width // 2

    # Vertical strokes
    draw.line([(w_center, 4), (w_center, height - 4)], fill=0, width=stroke_width)
    draw.line(
        [(w_center - 16, 6), (w_center - 16, height - 6)], fill=0, width=stroke_width
    )
    draw.line(
        [(w_center + 16, 6), (w_center + 16, height - 6)], fill=0, width=stroke_width
    )

    # Horizontal strokes
    draw.line([(4, h_center), (width - 4, h_center)], fill=0, width=stroke_width)
    draw.line([(4, 6), (width - 4, 6)], fill=0, width=stroke_width)
    draw.line([(4, height - 6), (width - 4, height - 6)], fill=0, width=stroke_width)

    return np.array(img)


def _make_page_with_chars(
    char_specs: list[dict],
) -> tuple[str, list[dict]]:
    """Create a temporary page image with characters drawn on it.

    Args:
        char_specs: List of dicts with keys: w, h, x0, y0, stroke_width.

    Returns:
        (temp_image_path, chars_list) where chars_list is compatible
        with the line dict's 'chars' field.
    """
    max_x = max(c["x0"] + c["w"] + 4 for c in char_specs)
    max_y = max(c["y0"] + c["h"] + 4 for c in char_specs)

    canvas = Image.new("L", (int(max_x) + 10, int(max_y) + 10), 255)
    chars = []

    for spec in char_specs:
        w, h = spec["w"], spec["h"]
        x0, y0 = spec["x0"], spec["y0"]
        x1, y1 = x0 + w, y0 + h
        sw = spec.get("stroke_width", 3)

        char_img = _make_char_image(w, h, sw)
        pil_img = Image.fromarray(char_img)
        canvas.paste(pil_img, (int(x0), int(y0)))

        chars.append(
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name)
    tmp.close()

    return tmp.name, chars


# ---------------------------------------------------------------------------
# Binarization
# ---------------------------------------------------------------------------


class TestBinarize:
    def test_text_on_white(self):
        """Dark text on white background should produce foreground pixels."""
        img = _make_char_image(48, 48, stroke_width=4)
        binary = _binarize(img)
        assert np.count_nonzero(binary) > 0
        assert binary[24, 24]

    def test_all_white_gives_no_foreground(self):
        """Uniform white image should produce no foreground pixels."""
        img = np.full((50, 50), 255, dtype=np.uint8)
        binary = _binarize(img)
        assert np.count_nonzero(binary) == 0

    def test_uniform_gray_handled(self):
        """Uniform gray image should not crash."""
        img = np.full((50, 50), 128, dtype=np.uint8)
        binary = _binarize(img)
        assert isinstance(binary, np.ndarray)


# ---------------------------------------------------------------------------
# Foreground height (font size)
# ---------------------------------------------------------------------------


class TestCharFgHeight:
    def test_measures_text_height(self):
        img = _make_char_image(48, 48, stroke_width=3)
        fh = _char_fg_height(img)
        assert fh is not None
        assert 35 <= fh <= 48


# ---------------------------------------------------------------------------
# refine_font_sizes_from_image integration
# ---------------------------------------------------------------------------


class TestRefineFontSizes:
    def test_handles_empty_lines(self):
        """Should not crash with empty lines list."""
        refine_font_sizes_from_image("/nonexistent.png", [])

    def test_no_chars_skipped_cleanly(self):
        """Lines without chars key should be skipped."""
        lines = [
            {"text": "test", "font_size": 16, "chars": []},
        ]
        refine_font_sizes_from_image("/nonexistent.png", lines)
        assert "_fg_height_px" not in lines[0]

    def test_fg_height_stored(self):
        """Font size refinement stores _fg_height_px on lines."""
        spec = {"w": 48, "h": 48, "stroke_width": 3}
        all_specs = []
        x, y = 10.0, 10.0
        for _ in range(5):
            all_specs.append({**spec, "x0": x, "y0": y})
            x += 52.0

        tmp_path, all_chars = _make_page_with_chars(all_specs)

        lines = [
            {
                "text": "test",
                "line_no": 0,
                "font_size": 16.0,
                "chars": all_chars,
            }
        ]

        refine_font_sizes_from_image(tmp_path, lines)
        Path(tmp_path).unlink()

        assert "_fg_height_px" in lines[0]
        assert lines[0]["_fg_height_px"] > 0
