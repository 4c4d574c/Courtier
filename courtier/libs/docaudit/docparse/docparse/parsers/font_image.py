"""Image binarization and foreground-height measurement helpers.

Crops of scanned text are binarized and their foreground pixel height is
measured — a direct physical measurement that is more accurate than OCR
box height.

All analysis is deterministic and requires no API calls.
"""

from __future__ import annotations

import numpy as np

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
