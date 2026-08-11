"""GB/T 9704-2012 standard calibration for Chinese government documents.

Provides standard font sizes, element format specifications, margins,
and calibration functions that snap detected values to the nearest
standard value within a defined tolerance.
"""

from __future__ import annotations

from docmodels.constants import STANDARD_FIRST_INDENT, STANDARD_MARGINS

# ---------------------------------------------------------------------------
# Standard font sizes: Chinese name -> points
# ---------------------------------------------------------------------------
STANDARD_FONT_SIZES: dict[str, float] = {
    "初号": 42.0,
    "小初": 36.0,
    "一号": 26.0,
    "小一": 24.0,
    "二号": 22.0,
    "小二": 18.0,
    "三号": 16.0,
    "小三": 15.0,
    "四号": 14.0,
    "小四": 12.0,
    "五号": 10.5,
    "小五": 9.0,
}

# Sorted unique point values extracted from STANDARD_FONT_SIZES.
STANDARD_SIZE_VALUES: list[float] = sorted(set(STANDARD_FONT_SIZES.values()))

# ---------------------------------------------------------------------------
# Element -> (font_family, font_size_pt, bold)
# ---------------------------------------------------------------------------
ELEMENT_FORMAT_MAP: dict[str, tuple[str, float, bool]] = {
    "issuing_logo": ("方正小标宋简体", 22.0, False),
    "issuing_number": ("仿宋", 16.0, False),
    "signatory": ("仿宋", 16.0, False),
    "title": ("方正小标宋简体", 22.0, False),
    "addressee": ("仿宋", 16.0, False),
    "body_text": ("仿宋", 16.0, False),
    "heading1": ("黑体", 16.0, False),
    "heading2": ("楷体", 16.0, False),
    "heading3": ("仿宋", 16.0, True),
    "heading4": ("仿宋", 16.0, False),
    "heading5": ("仿宋", 16.0, False),
    "attachment_note": ("仿宋", 16.0, False),
    "issuing_signature": ("仿宋", 16.0, False),
    "issue_date": ("仿宋", 16.0, False),
    "carbon_copy": ("仿宋", 14.0, False),
    "issuing_office": ("仿宋", 14.0, False),
    "distribution_date": ("仿宋", 14.0, False),
    "page_number": ("仿宋", 14.0, False),
}

# ---------------------------------------------------------------------------
# Tolerance constants
# ---------------------------------------------------------------------------
_FONT_SIZE_TOLERANCE_PT: float = 1.0
_MARGIN_TOLERANCE_MM: float = 2.0
_LINE_SPACING_TOLERANCE_PT: float = 0.5
_FIRST_INDENT_TOLERANCE_PT: float = 4.0
_FONT_SIZE_FACTOR: float = 0.75


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_element_format(element_name: str) -> tuple[str, float, bool] | None:
    """Return (font_family, font_size_pt, bold) for *element_name*, or None."""
    return ELEMENT_FORMAT_MAP.get(element_name)


def calibrate_font_size(detected_pt: float) -> float:
    """Snap *detected_pt* to the nearest standard size if within +/-1pt."""
    if detected_pt <= 0:
        return detected_pt
    best_std: float | None = None
    best_dist: float = float("inf")
    for std in STANDARD_SIZE_VALUES:
        dist = abs(detected_pt - std)
        if dist <= _FONT_SIZE_TOLERANCE_PT and dist < best_dist:
            best_std = std
            best_dist = dist
    return best_std if best_std is not None else detected_pt


def compute_font_size(
    box_height_px: float,
    img_height_px: float,
    a4_height_pt: float = 841.89,
    polys: list[tuple[float, float]] | None = None,
) -> float:
    """Compute calibrated font size from pixel measurements.

    Converts a bounding-box height (or quadrilateral polygon height) from
    image pixel space to standard font size in points, applying the 0.75
    correction factor for Chinese text and snapping to the nearest GB/T
    9704-2012 standard size.

    Args:
        box_height_px: Height of the text bounding box in pixels.
        img_height_px: Total image height in pixels.
        a4_height_pt: A4 page height in points (default 841.89).
        polys: Optional list of (x, y) corner points forming a quadrilateral.
            If provided with >= 4 points, the average of left and right edge
            heights is used instead of box_height_px.

    Returns:
        Calibrated font size in points, or 0.0 for zero/negative inputs.
    """
    if img_height_px <= 0:
        return 0.0

    height_px = box_height_px
    if polys is not None and len(polys) >= 4:
        left_height = abs(polys[3][1] - polys[0][1])
        right_height = abs(polys[2][1] - polys[1][1])
        height_px = (left_height + right_height) / 2

    if height_px <= 0:
        return 0.0

    raw_size_pt = height_px * (a4_height_pt / img_height_px) * _FONT_SIZE_FACTOR
    return calibrate_font_size(raw_size_pt)


def calibrate_margin(side: str, detected_mm: float) -> float:
    """Snap *detected_mm* to the standard margin for *side* if within +/-2mm."""
    standard = STANDARD_MARGINS.get(side)
    if standard is None:
        return detected_mm
    if abs(detected_mm - standard) <= _MARGIN_TOLERANCE_MM:
        return standard
    return detected_mm


def calibrate_line_spacing(detected_pt: float) -> float:
    """Return *detected_pt* unchanged.

    Line spacing is measured directly from the scan (OCR bounding box
    distances) rather than matched to standard sizes. Unlike font size
    and margins, line spacing has no discrete GB/T 9704-2012 standard
    values — it varies with document content and layout. The raw
    measurement from the scan is the best estimate available.
    """
    return detected_pt


def calibrate_first_indent(detected_pt: float) -> float:
    """Snap *detected_pt*: sub-tolerance noise (<4pt) to 0, near-standard to 32pt.

    OCR bounding boxes have 1-2px jitter, which shows up as tiny non-zero
    indents (0.4-3.3pt) on lines that are actually flush with the margin.
    Anything below the snap tolerance is measurement noise, not a real
    indent, and is reported as 0.0.
    """
    if detected_pt <= 0:
        return detected_pt
    if detected_pt < _FIRST_INDENT_TOLERANCE_PT:
        return 0.0
    if abs(detected_pt - STANDARD_FIRST_INDENT) <= _FIRST_INDENT_TOLERANCE_PT:
        return STANDARD_FIRST_INDENT
    return detected_pt
