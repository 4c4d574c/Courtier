"""Image-based font size refinement using per-character bounding box crops.

Crops individual characters from the page image and measures foreground
pixel height from binarized crops — a direct physical measurement that
is more accurate than OCR box height.

All analysis is deterministic and requires no API calls.
"""

from __future__ import annotations

import statistics
from typing import Any

import numpy as np
from PIL import Image

# Minimum number of CJK-like characters needed for reliable per-line analysis.
_MIN_CJK_CHARS: int = 3

# Characters whose W/H ratio falls in this range are considered CJK-like
# (excludes punctuation, digits, and other narrow symbols).
_CJK_RATIO_MIN: float = 0.55
_CJK_RATIO_MAX: float = 1.45

# Crop margin (pixels) added around the bbox to avoid edge clipping.
_CROP_MARGIN: int = 2


# ---------------------------------------------------------------------------
# Image loading and cropping
# ---------------------------------------------------------------------------


def _load_image(image_path: str) -> np.ndarray:
    """Load page image as a grayscale numpy array."""
    img = Image.open(image_path)
    if img.mode != "L":
        img = img.convert("L")
    return np.array(img)


def _crop_char(
    page: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> np.ndarray | None:
    """Crop a single character from the page image.

    Returns a 2D uint8 numpy array, or None if the crop region is invalid.
    """
    h, w = page.shape
    left = max(0, int(x0) - _CROP_MARGIN)
    top = max(0, int(y0) - _CROP_MARGIN)
    right = min(w, int(x1) + _CROP_MARGIN)
    bottom = min(h, int(y1) + _CROP_MARGIN)

    if right <= left or bottom <= top:
        return None

    return page[top:bottom, left:right]


# ---------------------------------------------------------------------------
# Binarization
# ---------------------------------------------------------------------------


def _binarize(gray: np.ndarray) -> np.ndarray:
    """Binarize grayscale image for text-on-paper character crops.

    Uses mean-minus-k-std thresholding: for character crops from scanned
    documents, text pixels are always darker than the paper background.

    Returns a boolean array where True = foreground (text) pixels.
    """
    if gray.size == 0:
        return np.zeros((0, 0), dtype=bool)

    mean_val = float(gray.mean())
    std_val = float(gray.std()) if gray.std() > 0 else 1.0

    # Text pixels are the dark outliers below the background mean.
    # Clamp threshold to [30, 230] to handle extreme cases.
    thresh = max(30.0, min(230.0, mean_val - 1.2 * std_val))

    return gray < thresh


# ---------------------------------------------------------------------------
# Font size from foreground height
# ---------------------------------------------------------------------------


def _char_fg_height(crop: np.ndarray) -> int | None:
    """Measure actual foreground pixel height from binarized crop."""
    if crop.size < 25:
        return None
    binary = _binarize(crop)
    ys, _xs = np.nonzero(binary)
    if len(ys) == 0:
        return None
    return int(ys.max() - ys.min() + 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def refine_font_sizes_from_image(
    image_path: str,
    lines: list[dict[str, Any]],
) -> None:
    """Refine font_size using foreground pixel height from character crops.

    For each line with per-character bounding boxes, crops characters
    from the page image and measures the actual foreground pixel height.
    Stores the result as _fg_height_px on each line dict for the caller
    to convert via the calibration pipeline.

    Only fills font_size fields that are not already set.

    Args:
        image_path: Path to the page image file (PNG recommended).
        lines: List of extracted line dicts. Must have 'chars' key with
               per-character bbox data for at least some lines.
    """
    if not lines:
        return

    has_chars = any(line.get("chars") for line in lines)
    if not has_chars:
        return

    page = _load_image(image_path)

    for line in lines:
        chars = line.get("chars", [])
        if not chars:
            continue

        heights: list[int] = []
        for c in chars[:20]:  # sample first 20 chars for performance
            w = c["x1"] - c["x0"]
            h = c["y1"] - c["y0"]
            if h <= 0 or w <= 0:
                continue
            ratio = w / h
            if not (_CJK_RATIO_MIN <= ratio <= _CJK_RATIO_MAX):
                continue
            crop = _crop_char(page, c["x0"], c["y0"], c["x1"], c["y1"])
            if crop is None:
                continue
            fh = _char_fg_height(crop)
            if fh is not None:
                heights.append(fh)

        if len(heights) >= _MIN_CJK_CHARS:
            line["_fg_height_px"] = statistics.median(heights)
