"""Font detection refinement for scanned documents.

Provides LLM font info merging and chars-per-line font size estimation
for the scanned document pipeline.
"""

from __future__ import annotations

import statistics
from typing import Any

from .._constants import MM_TO_PT as _MM_TO_PT
from ..calibration import calibrate_font_size

_A4_WIDTH_MM: float = 210.0

# GB/T 9704-2012 standard margins: left 28mm, right 26mm — the fallback
# when measured margins are implausible (tightly cropped scans where text
# extends to the image edge report ~0mm).
_STD_LEFT_MM: float = 28.0
_STD_RIGHT_MM: float = 26.0

# Plausible per-side margin range (mm) for A4 official documents.  Only
# measured margins inside it are trusted for the 版心 estimate.
_PLAUSIBLE_MARGIN_MM: tuple[float, float] = (15.0, 40.0)


def _estimate_available_width_mm(page_metrics: list[dict[str, Any]]) -> float:
    """Available text width (版心) in mm from measured margins when plausible.

    Each side independently uses the document-level median of the measured
    margins that fall inside _PLAUSIBLE_MARGIN_MM; a side without plausible
    measurements falls back to the GB/T standard (28/26mm).  The fixed
    standard width is wrong for documents typeset with narrower real
    margins (e.g. 22/17mm): their wider 版心 packs more chars per line,
    and the chars-per-line estimate comes out one font size too small.
    """
    lo, hi = _PLAUSIBLE_MARGIN_MM
    lefts: list[float] = []
    rights: list[float] = []
    for pm in page_metrics:
        margin = pm.get("margin")
        if margin is None:
            continue
        if lo <= margin.left_margin <= hi:
            lefts.append(margin.left_margin)
        if lo <= margin.right_margin <= hi:
            rights.append(margin.right_margin)
    left = statistics.median(lefts) if lefts else _STD_LEFT_MM
    right = statistics.median(rights) if rights else _STD_RIGHT_MM
    return _A4_WIDTH_MM - left - right


def merge_font_info(
    lines: list[dict[str, Any]],
    font_info: dict[int, dict[str, Any]],
) -> None:
    """Merge LLM-recognized font info into extracted lines in place.

    Only overwrites font fields that are currently empty/default,
    preserving any existing font data from OCR or other sources.

    Args:
        lines: List of line dicts to update (modified in place).
        font_info: Dict mapping line_no to font properties.
    """
    for line in lines:
        line_no = line.get("line_no", -1)
        if line_no not in font_info:
            continue
        info = font_info[line_no]
        if info.get("font_family") and not line.get("font_family"):
            line["font_family"] = info["font_family"]
        if info.get("font_weight") and not line.get("font_weight"):
            line["font_weight"] = info["font_weight"]
        if info.get("font_style") and not line.get("font_style"):
            line["font_style"] = info["font_style"]


def refine_font_size_by_chars_per_line(
    page_metrics: list[dict[str, Any]],
) -> None:
    """Determine body text font size from chars-per-line (primary method).

    The number of characters per line directly reflects the font size for
    Chinese official documents.  The available text width (版心) is taken
    from the document's measured margins when plausible, falling back to
    the GB/T standard margins — see _estimate_available_width_mm.
    First-line indented lines are excluded from the estimate because the
    indent reduces the character count by ~2 chars, skewing the result.

    After computing the document-level estimate, body_text lines always
    get this value (it's the primary source). Non-body-text lines only
    get it as a fallback when OCR gives no value or is clearly wrong.

    Args:
        page_metrics: List of per-page metrics dicts. Each must contain
            "lines", "margin", and "indent_map". Modified in place.
    """
    # Available width comes from measured margins when they are plausible,
    # falling back per side to the GB/T standard (28/26mm) — see
    # _estimate_available_width_mm.
    available_mm = _estimate_available_width_mm(page_metrics)

    # Minimum indent (pt) to consider a line as having first-line indent.
    # Standard 2-char indent is ~32pt; threshold of 10pt safely separates.
    _INDENT_THRESHOLD_PT: float = 10.0

    # Collect character counts from non-indented body-text lines.
    all_char_counts: list[int] = []

    for pm in page_metrics:
        lines: list[dict[str, Any]] = pm.get("lines", [])
        indent_map: dict[int, float] = pm.get("indent_map", {})
        if not lines:
            continue

        for idx, line in enumerate(lines):
            if line.get("outline_level", "") not in ("body_text", "others"):
                continue
            text = line.get("text", "")
            if len(text) < 10:
                continue
            # Skip indented lines — indent reduces char count without
            # changing font size. indent_map keys are positions in the
            # extracted_lines list (same order as rec_boxes).
            if indent_map.get(idx, 0.0) > _INDENT_THRESHOLD_PT:
                continue
            all_char_counts.append(len(text))

    if len(all_char_counts) < 3:
        return

    median_chars = statistics.median(all_char_counts)
    if median_chars <= 0:
        return

    estimated_font_size_mm = available_mm / median_chars
    estimated_font_size_pt = estimated_font_size_mm * _MM_TO_PT
    doc_estimate = calibrate_font_size(estimated_font_size_pt)

    # Only apply when the estimate is plausible for Chinese official documents.
    if not (10.0 <= doc_estimate <= 24.0):
        return

    for pm in page_metrics:
        lines = pm.get("lines", [])
        for line in lines:
            outline = line.get("outline_level", "")
            pixel_fs = line.get("font_size", 0.0)

            if outline == "body_text":
                # Chars-per-line is the primary source for body text.
                line["font_size"] = round(doc_estimate, 1)
            elif pixel_fs <= 0:
                # Only fill in missing values for non-body-text lines.
                # Don't override — OCR box height is more reliable for
                # titles/headings than chars-per-line.
                line["font_size"] = round(doc_estimate, 1)
