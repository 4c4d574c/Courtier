"""Page region splitting for document structure classification.

Splits extracted text lines into three GB/T 9704 regions:
- Header (版头): 份号、密级、紧急程度、发文机关标志、发文字号、签发人、红色分割线
- Body (主体): 标题、主送机关、正文、附件说明、署名、成文日期、印章
- Footer (版记): 抄送机关、印发机关、印发日期、页码

Two splitting strategies:
- Content-based: for DOCX (no Y coordinates), uses keywords and patterns
- Position-based: for PDF/scanned (has Y coordinates), uses Y-gap heuristics
"""

from __future__ import annotations

from typing import Any

from .rule_patterns import (
    FOOTER_KEYWORDS,
    ISSUING_NUMBER_PATTERN,
    SIGNATORY_PATTERN,
    STYLE_NAME_TO_FIELD,
)


def split_docx_regions(
    lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split DOCX lines into header/body/footer regions by content patterns.

    DOCX has no Y coordinates, so we use content-based detection:
    1. Header ends after the last "header marker" line (发文字号/签发人).
    2. Footer starts at the first "footer keyword" line (抄送/印发).
    3. Everything in between is body.

    Args:
        lines: Extracted text lines with text/font metadata.

    Returns:
        Tuple of (header_lines, body_lines, footer_lines).
    """
    if not lines:
        return [], [], []

    header_end = _content_based_header_end(lines, large_font_threshold=22.0)
    footer_start = len(lines)

    # Find footer start: first line containing footer keywords,
    # or a line whose style_name indicates it's past the body region
    for i, line in enumerate(lines):
        text = line.get("text", "")
        style_name = line.get("style_name", "")
        style_field = STYLE_NAME_TO_FIELD.get(style_name, "")
        if any(kw in text for kw in FOOTER_KEYWORDS):
            footer_start = i
            break
        if style_field in ("issuing_signature", "issue_date") and i > header_end:
            footer_start = i
            break

    # Safety: footer must come after header
    if footer_start < header_end:
        footer_start = len(lines)

    header = lines[:header_end]
    body = lines[header_end:footer_start]
    footer = lines[footer_start:]

    return header, body, footer


def split_pdf_regions(
    lines: list[dict[str, Any]],
    page_height: float = 841.89,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split PDF/scanned lines into header/body/footer by Y coordinates.

    Uses Y-gap heuristics:
    1. Find the largest Y-gap above the page midpoint → header/body boundary.
    2. Find the largest Y-gap below 70% of page height → body/footer boundary.
    3. Fall back to content-based detection if gaps are insufficient.

    Args:
        lines: Extracted text lines with x0/y0/x1/y1 position data.
        page_height: Page height in points (default A4 = 841.89).

    Returns:
        Tuple of (header_lines, body_lines, footer_lines).
    """
    if not lines:
        return [], [], []

    has_position = any(line.get("y0", 0) > 0 or line.get("y1", 0) > 0 for line in lines)
    if not has_position:
        return split_docx_regions(lines)

    # Sort lines by y0 for gap analysis
    sorted_lines = sorted(lines, key=lambda line: line.get("y0", 0))
    n = len(sorted_lines)

    if n < 2:
        return [], sorted_lines, []

    # Compute consecutive Y gaps
    gaps: list[tuple[int, float]] = []
    for i in range(n - 1):
        curr_bottom = sorted_lines[i].get("y1", 0)
        next_top = sorted_lines[i + 1].get("y0", 0)
        gap = max(0.0, next_top - curr_bottom)
        gaps.append((i, gap))

    if not gaps:
        return [], sorted_lines, []

    # Median gap for threshold
    sorted_gap_values = sorted(g for _, g in gaps)
    median_gap = sorted_gap_values[len(sorted_gap_values) // 2]
    gap_threshold = max(median_gap * 2.0, 5.0)

    # Find header/body boundary: largest gap above midpoint
    midpoint_y = page_height * 0.4
    header_boundary_idx = -1
    header_boundary_gap = 0.0
    for i, (line_idx, gap) in enumerate(gaps):
        line_y = sorted_lines[line_idx].get("y0", 0)
        if line_y < midpoint_y and gap > gap_threshold and gap > header_boundary_gap:
            header_boundary_idx = line_idx
            header_boundary_gap = gap

    # Find body/footer boundary: largest gap below 70% of page
    footer_zone_y = page_height * 0.70
    footer_boundary_idx = -1
    footer_boundary_gap = 0.0
    for i, (line_idx, gap) in enumerate(gaps):
        line_y = sorted_lines[line_idx].get("y0", 0)
        if (
            line_y >= footer_zone_y
            and gap > gap_threshold
            and gap > footer_boundary_gap
        ):
            footer_boundary_idx = line_idx
            footer_boundary_gap = gap

    # Split based on boundaries
    if header_boundary_idx >= 0:
        header = sorted_lines[: header_boundary_idx + 1]
        remaining = sorted_lines[header_boundary_idx + 1 :]
    else:
        header = []
        remaining = sorted_lines

    if footer_boundary_idx >= 0:
        # Find the same line in remaining
        footer_boundary_y = sorted_lines[footer_boundary_idx].get("y0", 0)
        split_point = 0
        for j, line in enumerate(remaining):
            if line.get("y0", 0) >= footer_boundary_y:
                split_point = j
                break
        body = remaining[:split_point]
        footer = remaining[split_point:]
    else:
        body = remaining
        footer = []

    # Content-based fallback: if Y-gap analysis produced empty regions,
    # use keyword/pattern detection (same strategy as DOCX path).
    all_sorted = header + body + footer
    if not header and all_sorted:
        cb_header_end = _content_based_header_end(all_sorted)
        if cb_header_end > 0:
            header = all_sorted[:cb_header_end]
            body = all_sorted[cb_header_end:]
            footer = []

    if not footer and body:
        cb_footer_start = _content_based_footer_start(body)
        if cb_footer_start < len(body):
            footer = body[cb_footer_start:]
            body = body[:cb_footer_start]

    return header, body, footer


def _content_based_header_end(
    lines: list[dict[str, Any]],
    large_font_threshold: float = 18.0,
) -> int:
    header_end = 0
    for i, line in enumerate(lines):
        text = line.get("text", "")
        style_name = line.get("style_name", "")
        style_field = STYLE_NAME_TO_FIELD.get(style_name, "")
        if ISSUING_NUMBER_PATTERN.match(text):
            header_end = i + 1
        elif SIGNATORY_PATTERN.match(text):
            header_end = i + 1
        elif style_field in ("issuing_logo", "issuing_number", "signatory"):
            header_end = i + 1
    if header_end == 0 and lines:
        text = lines[0].get("text", "")
        font_size = lines[0].get("font_size", 0.0)
        alignment = lines[0].get("alignment", "")
        if font_size >= large_font_threshold and alignment == "center":
            header_end = 1
    return header_end


def _content_based_footer_start(
    lines: list[dict[str, Any]],
) -> int:
    for i, line in enumerate(lines):
        text = line.get("text", "")
        if any(kw in text for kw in FOOTER_KEYWORDS):
            return i
    return len(lines)
