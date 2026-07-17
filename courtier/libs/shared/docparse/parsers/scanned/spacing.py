"""Spacing normalization for scanned documents.

Provides page-content spacing merging, document-level body line spacing
normalization, histogram clustering, and cross-page continuation detection
for the scanned document pipeline.
"""

from __future__ import annotations

from typing import Any

from docmodels import PageContent

from ..calibration import calibrate_line_spacing
from ..spacing import compute_body_line_spacing
from .structure import collect_all_paragraphs

_SENTENCE_END = frozenset({"。", "！", "？", ".", "!", "?", "；", ";", "："})


def merge_spacing_into_page_content(
    page_content: PageContent,
    spacing_map: dict[int, dict[str, float]],
    indent_map: dict[int, float],
    left_right_indent_map: dict[int, dict[str, float]] | None = None,
) -> None:
    """Merge computed spacing and indent into PageContent paragraphs.

    Updates all Paragraph objects within the PageContent with the
    spacing and indent values computed from bounding box analysis.

    Args:
        page_content: The PageContent to update in place.
        spacing_map: Line_no to spacing info mapping.
        indent_map: Line_no to first_indent mapping.
        left_right_indent_map: Optional line_no to {left_indent, right_indent}
            mapping for left/right indentation.
    """
    paragraphs = collect_all_paragraphs(page_content)
    for para in paragraphs:
        if not para.elements:
            continue
        # Use first element's line_no to look up spacing
        first_line_no = para.elements[0].font.line_no
        if first_line_no in spacing_map:
            sp = spacing_map[first_line_no]
            para.space_before = sp["space_before"]
            para.space_after = sp["space_after"]
            para.line_spacing = sp["line_spacing"]
        if first_line_no in indent_map:
            para.first_indent = indent_map[first_line_no]
        if left_right_indent_map and first_line_no in left_right_indent_map:
            lr = left_right_indent_map[first_line_no]
            para.left_indent = lr["left_indent"]
            para.right_indent = lr["right_indent"]


def normalize_body_line_spacing(
    page_metrics: list[dict[str, Any]],
) -> None:
    """Normalize body line spacing across pages using document-level clustering.

    Body text line spacing is a document-level property — it should be
    consistent across all pages. Per-page histogram clustering can be
    unreliable on pages with few lines or mixed content. This function
    collects all gaps (in pt) across all pages, clusters them once at
    the document level, and applies the consensus to every page.

    For each page's spacing_map, this updates:
    - line_spacing: replaced with document-level calibrated value
    - space_before / space_after: re-classified using document-level
      paragraph break threshold

    Args:
        page_metrics: List of per-page metrics dicts. Each must contain
            "lines", "spacing_map", "img_height", "a4_height_pt".
            Modified in place.
    """
    if len(page_metrics) < 2:
        return

    # Collect gaps in pt from all pages — both bottom-to-top gaps
    # (for paragraph break detection) and top-to-top distances
    # (for true line spacing).
    all_gaps_pt: list[float] = []
    all_top_distances_pt: list[float] = []
    page_gap_data: list[list[tuple[int, float, float]]] = []  # (idx, gap_pt, curr_height_pt)

    for pm in page_metrics:
        lines = pm.get("lines", [])
        img_height = pm.get("img_height", 0)
        a4_height_pt = pm.get("a4_height_pt", 841.89)

        if len(lines) < 2 or img_height <= 0:
            page_gap_data.append([])
            continue

        scale_y = a4_height_pt / img_height

        # Sort by Y to get consecutive gaps
        indexed_lines = sorted(enumerate(lines), key=lambda x: x[1].get("y0", 0))
        gaps: list[tuple[int, float, float]] = []
        for i in range(len(indexed_lines) - 1):
            curr_idx = indexed_lines[i][0]
            curr_bottom = indexed_lines[i][1].get("y1", 0)
            curr_top = indexed_lines[i][1].get("y0", 0)
            next_top = indexed_lines[i + 1][1].get("y0", 0)
            gap_px = max(0.0, next_top - curr_bottom)
            gap_pt = gap_px * scale_y
            curr_height_pt = max(0.0, curr_bottom - curr_top) * scale_y
            gaps.append((curr_idx, gap_pt, curr_height_pt))
            all_gaps_pt.append(gap_pt)
            all_top_distances_pt.append(max(0.0, next_top - curr_top) * scale_y)

        page_gap_data.append(gaps)

    if not all_gaps_pt:
        return

    # Document-level line spacing: cluster top-to-top distances
    doc_top_cluster_center_pt, _ = histogram_cluster_pt(all_top_distances_pt)

    if doc_top_cluster_center_pt <= 0.0:
        return

    doc_line_spacing = calibrate_line_spacing(doc_top_cluster_center_pt)

    # Document-level paragraph break threshold: cluster bottom-to-top gaps
    doc_gap_cluster_center_pt, _ = histogram_cluster_pt(all_gaps_pt)
    doc_para_threshold_pt = max(doc_gap_cluster_center_pt * 1.8, doc_gap_cluster_center_pt + 5.0)

    # Apply document-level values to each page
    for page_idx, pm in enumerate(page_metrics):
        gaps = page_gap_data[page_idx]
        spacing_map: dict[int, dict[str, float]] = pm.get("spacing_map", {})
        if not gaps or not spacing_map:
            continue

        for i, (line_idx, gap_pt, curr_height_pt) in enumerate(gaps):
            if line_idx not in spacing_map:
                continue

            is_break = gap_pt > doc_para_threshold_pt
            if is_break:
                normal_visual_gap = max(0.0, doc_line_spacing - curr_height_pt)
                space_after = max(0.0, gap_pt - normal_visual_gap)
            else:
                space_after = 0.0

            spacing_map[line_idx]["line_spacing"] = round(doc_line_spacing, 2)
            spacing_map[line_idx]["space_after"] = round(space_after, 2)
            spacing_map[line_idx]["space_before"] = 0.0

        # Fix last line: space_after is always 0 (no next line on page)
        if gaps:
            last_idx = gaps[-1][0]
            if last_idx in spacing_map:
                spacing_map[last_idx]["space_after"] = 0.0
                spacing_map[last_idx]["line_spacing"] = round(doc_line_spacing, 2)


def histogram_cluster_pt(
    gaps_pt: list[float],
    bin_width: float = 2.0,
) -> tuple[float, float]:
    """Find the dominant gap cluster in pt units via histogram.

    Delegates to ``compute_body_line_spacing`` with 2.5x-median outlier
    filtering and 30 % minimum-cluster confidence check enabled.

    Args:
        gaps_pt: Gap values in points.
        bin_width: Histogram bin width in points.

    Returns:
        (cluster_center_pt, cluster_width).
    """
    return compute_body_line_spacing(
        gaps_pt,
        bin_width=bin_width,
        outlier_median_multiplier=2.5,
        min_cluster_ratio=0.3,
    )


def adjust_cross_page_spacing(
    page_metrics: list[dict[str, Any]],
) -> None:
    """Adjust spacing at page boundaries for multi-page scanned documents.

    Since each page's spacing is computed in isolation from its own OCR boxes,
    page-boundary spacing values are meaningless without cross-page context.
    This function detects paragraph continuations across pages and corrects
    the spacing accordingly.

    For a paragraph that continues from page N to page N+1:
    - Page N's last-line space_after is cleared (paragraph keeps going)
    - Page N+1's first-line space_before is cleared (continued from above)

    Args:
        page_metrics: List of per-page metrics dicts, each containing
            "lines" and "spacing_map". Modified in place.
    """
    if len(page_metrics) < 2:
        return

    for i in range(len(page_metrics) - 1):
        curr = page_metrics[i]
        next_pm = page_metrics[i + 1]

        curr_lines: list[dict[str, Any]] = curr.get("lines", [])
        next_lines: list[dict[str, Any]] = next_pm.get("lines", [])
        curr_spacing: dict[int, dict[str, float]] = curr.get("spacing_map", {})
        next_spacing: dict[int, dict[str, float]] = next_pm.get("spacing_map", {})

        if not curr_lines or not next_lines:
            continue

        # Find last line of current page by Y position
        curr_indexed = sorted(enumerate(curr_lines), key=lambda x: x[1].get("y0", 0))
        last_idx, last_line = curr_indexed[-1]

        # Find first line of next page by Y position
        next_indexed = sorted(enumerate(next_lines), key=lambda x: x[1].get("y0", 0))
        first_idx, first_line = next_indexed[0]

        is_continuation = _is_cross_page_continuation(last_line, first_line)

        if is_continuation:
            if last_idx in curr_spacing:
                curr_spacing[last_idx]["space_after"] = 0.0
            if first_idx in next_spacing:
                next_spacing[first_idx]["space_before"] = 0.0


def _is_cross_page_continuation(
    last_line: dict[str, Any],
    first_line: dict[str, Any],
) -> bool:
    """Determine whether two lines across a page boundary form a continuation.

    A continuation means the paragraph that ends on page N continues
    naturally on page N+1, without a paragraph break or section change.

    Returns True if the lines share the same outline level and the
    last line does not end with sentence-ending punctuation (suggesting
    the sentence continues).
    """
    if last_line.get("outline_level") != first_line.get("outline_level"):
        return False

    last_text = last_line.get("text", "")
    if not last_text:
        return False

    stripped = last_text.rstrip()
    if not stripped:
        return False
    if stripped[-1] in _SENTENCE_END:
        return False

    last_font = last_line.get("font_family", "")
    first_font = first_line.get("font_family", "")
    if last_font and first_font and last_font != first_font:
        return False

    return True
