"""Tests for image binarization and foreground-height measurement."""

import numpy as np
from docparse.parsers.font_image import (
    _binarize,
    _char_fg_height,
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
    draw.line([(w_center - 16, 6), (w_center - 16, height - 6)], fill=0, width=stroke_width)
    draw.line([(w_center + 16, 6), (w_center + 16, height - 6)], fill=0, width=stroke_width)

    # Horizontal strokes
    draw.line([(4, h_center), (width - 4, h_center)], fill=0, width=stroke_width)
    draw.line([(4, 6), (width - 4, 6)], fill=0, width=stroke_width)
    draw.line([(4, height - 6), (width - 4, height - 6)], fill=0, width=stroke_width)

    return np.array(img)


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
