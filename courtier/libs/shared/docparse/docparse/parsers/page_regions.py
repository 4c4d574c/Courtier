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
    CLASSIFICATION_PATTERN,
    COPY_NUMBER_PATTERN,
    FONT_SIZE_THRESHOLDS,
    FOOTER_KEYWORDS,
    ISSUE_DATE_PATTERN,
    ISSUE_DATE_PATTERN_OCR,
    ISSUING_NUMBER_PATTERN,
    ISSUING_NUMBER_PATTERN_OCR,
    SIGNATORY_PATTERN,
    STYLE_NAME_TO_FIELD,
    URGENCY_PATTERN,
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

    # Explicit Word header/footer hints: docx_parser tags lines extracted
    # from section headers/footers ("_word_header"/"_word_footer"); they
    # must land in their own region regardless of content heuristics.
    # By construction header hints always precede footer hints.
    hinted_header_end = max(
        (i + 1 for i, line in enumerate(lines) if line.get("_word_header")),
        default=0,
    )
    hinted_footer_start = min(
        (i for i, line in enumerate(lines) if line.get("_word_footer")),
        default=len(lines),
    )
    header_end = max(header_end, hinted_header_end)
    footer_start = min(footer_start, hinted_footer_start)

    # Safety: footer must come after header. Hinted boundaries are
    # authoritative (the DOCX itself declares where these lines live);
    # without hints, drop the footer as before.
    if footer_start < header_end:
        if hinted_header_end > 0 or hinted_footer_start < len(lines):
            header_end = hinted_header_end
            footer_start = hinted_footer_start
        else:
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
    1. Find the largest Y-gap above the page midpoint → header/body boundary,
       but only when the region above it shows real 版头 markers (续页无版头).
    2. Find the largest Y-gap below 70% of page height → body/footer boundary,
       skipping gaps that would swallow the 成文日期 (a body element) into
       the footer.
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

    # 版头边界仅在上方区域确有版头标志时成立。续页没有版头：短续页上
    # 「正文/日期→版记」的大空隙若位于中点之上，会成为最大空隙而被误当
    # 版头/主体边界，把该页正文行划进 header（最终落入 unclassified 兜底）。
    if header_boundary_idx >= 0 and not _has_header_markers(
        sorted_lines[: header_boundary_idx + 1]
    ):
        header_boundary_idx = -1

    # Find body/footer boundary: largest gap below 70% of page
    footer_zone_y = page_height * 0.70
    footer_boundary_idx = -1
    footer_boundary_gap = 0.0
    for i, (line_idx, gap) in enumerate(gaps):
        line_y = sorted_lines[line_idx].get("y0", 0)
        if line_y >= footer_zone_y and gap > gap_threshold and gap > footer_boundary_gap:
            # 成文日期是主体要素：边界切分从边界行起算 footer，成文日期
            # 紧贴版记空隙（边界行即日期，或日期上方还有更大空隙）时会被
            # 吞进 footer（多认一个 distribution_date）——跳过该候选，
            # 版记改由关键词兜底识别。
            if _footer_boundary_swallows_issue_date(sorted_lines, line_idx):
                continue
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


def _has_header_markers(lines: list[dict[str, Any]]) -> bool:
    """True if any line shows a GB/T 版头 marker.

    标志：发文字号/签发人/份号/密级/紧急程度的文本模式、Word 样式映射的
    版头字段，或 ≥22pt 的大字行（发文机关标志；PDF 行无 alignment，
    不能依赖居中判定）。续页正文/成文日期均不满足这些条件。
    """
    for line in lines:
        text = line.get("text", "")
        style_field = STYLE_NAME_TO_FIELD.get(line.get("style_name", ""), "")
        if style_field in ("issuing_logo", "issuing_number", "signatory"):
            return True
        if (
            ISSUING_NUMBER_PATTERN.match(text)
            or ISSUING_NUMBER_PATTERN_OCR.match(text)
            or SIGNATORY_PATTERN.match(text)
            or COPY_NUMBER_PATTERN.match(text)
            or CLASSIFICATION_PATTERN.match(text)
            or URGENCY_PATTERN.match(text)
        ):
            return True
        if line.get("font_size", 0.0) >= FONT_SIZE_THRESHOLDS["issuing_logo_min"]:
            return True
    return False


def _footer_boundary_swallows_issue_date(
    sorted_lines: list[dict[str, Any]],
    line_idx: int,
) -> bool:
    """True if a footer boundary at *line_idx* would put a 成文日期 into
    the footer ahead of the real 版记 content.

    The footer split is inclusive (footer starts at the boundary line), so
    the boundary line itself and everything below it becomes footer.  Scan
    from the boundary line down to the first footer-keyword line
    （抄送/印发/印刷/翻印）: an issue date in that range is a body element
    being swallowed （多认一个 distribution_date）; lines past the first
    keyword line are the legitimate 版记.
    """
    for line in sorted_lines[line_idx:]:
        text = line.get("text", "")
        if any(kw in text for kw in FOOTER_KEYWORDS):
            return False  # 到达真正的版记，其前未成文日期
        if ISSUE_DATE_PATTERN.match(text) or ISSUE_DATE_PATTERN_OCR.match(text):
            return True
    return False


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
