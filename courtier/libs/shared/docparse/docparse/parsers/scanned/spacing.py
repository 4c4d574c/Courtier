"""Spacing normalization for scanned documents.

Provides page-content spacing merging, document-level body line spacing
normalization, and histogram clustering for the scanned document pipeline.
"""

from __future__ import annotations

from typing import Any

from docmodels import PageContent

from ..calibration import calibrate_line_spacing
from ..spacing import compute_body_line_spacing
from .structure import collect_all_paragraphs


def merge_spacing_into_page_content(
    page_content: PageContent,
    spacing_map: dict[int, dict[str, float | None]],
    indent_map: dict[int, float],
    left_right_indent_map: dict[int, dict[str, float]] | None = None,
) -> None:
    """Merge computed spacing and indent into PageContent paragraphs.

    Updates all Paragraph objects within the PageContent with the
    spacing and indent values computed from bounding box analysis.

    space_before / line_spacing are looked up by the paragraph's FIRST
    line; space_after by its LAST line (the line with the largest y0) —
    for a multi-line paragraph the measured space_after is booked on the
    last line, not the first.

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
            para.line_spacing = sp["line_spacing"]
        # space_after lives on the paragraph's last line (max y0, robust
        # to line_indices that are not in reading order).
        last_line_no = max(para.elements, key=lambda e: e.position.y0).font.line_no
        if last_line_no in spacing_map:
            para.space_after = spacing_map[last_line_no]["space_after"]
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
    - space_after: re-classified using document-level paragraph break
      threshold (None for the page-last line — unmeasurable)
    - space_before: always None (unmeasurable for scanned lines)

    跨页续接段的页末 space_after / 页首 space_before 因跨页间隙不可测量
    而恒为 None（未测得），不做跨页修正。

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
        spacing_map: dict[int, dict[str, float | None]] = pm.get("spacing_map", {})
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
            spacing_map[line_idx]["space_before"] = None

        # Page-last line: space_after is unmeasurable (no next line on
        # this page) — None (未测得), not a guessed 0.0.  Also apply the
        # document-level line spacing, which the gap loop above skips
        # for this line.  Note the true last line is the one with the
        # largest y0, not gaps[-1] (which is the second-to-last line).
        lines = pm.get("lines", [])
        if lines:
            last_idx = max(range(len(lines)), key=lambda i: lines[i].get("y0", 0))
            if last_idx in spacing_map:
                spacing_map[last_idx]["space_after"] = None
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
