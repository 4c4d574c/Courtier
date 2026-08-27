"""Structure recognizer: rule-engine classification → PageContent conversion.

Converts ``StructureRuleEngine`` classification results into the
header/body/footer PageContent model, merges consecutive body lines into
paragraphs, and estimates paragraph spacing from element positions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from docmodels import (
    Body,
    Font,
    Footer,
    Header,
    LineElement,
    Margin,
    PageContent,
    Paragraph,
    Position,
)

from .rules import ClassifiedLine

logger = logging.getLogger(__name__)

# Paragraph-level spacing/indent keys carried on extracted line dicts.
# Only the DOCX parser populates them (direct python-docx reads); PDF
# spacing is estimated from positions and scanned spacing is merged by
# merge_spacing_into_page_content instead.
_PARA_SPACING_KEYS = (
    "space_before",
    "space_after",
    "line_spacing",
    "first_indent",
    "left_indent",
    "right_indent",
)

# 版头槽位后校验（见 _validate_header_slots）：密级槽的文本特征（密级/绝密/
# 机密/秘密/▲/★）与发文字号槽的文本特征（括号年份，如 〔2025〕/[2026】）。
_SECRECY_TEXT_RE = re.compile(r"密级|绝密|机密|秘密|▲|★")
_ISSUING_NUMBER_TEXT_RE = re.compile(r"[〔\[【(（][^〕\]】)）]{0,6}(?:19|20)\d{2}")


def _build_paragraph(
    text: str,
    line_indices: list[int],
    extracted_lines: list[dict[str, Any]],
    outline_level: str = "others",
) -> Paragraph:
    """Build a Paragraph from classification results and extracted line data.

    Args:
        text: The text content of the paragraph.
        line_indices: Line indices into extracted_lines.
        extracted_lines: Original extracted line data with position/font info.
        outline_level: Outline level for this paragraph.

    Returns:
        A Paragraph with elements populated from matching extracted lines.
    """
    alignment = _get_alignment_from_indices(line_indices, extracted_lines)

    if not text and not line_indices:
        return Paragraph(outline_level=outline_level, alignment=alignment)

    elements: list[LineElement] = []
    # Paragraph-level spacing/indent is taken from the first matched line
    # (only DOCX lines carry these keys; other sources leave them unset).
    spacing: dict[str, float] = {}
    first_line_taken = False
    for idx in line_indices:
        if idx < 0 or idx >= len(extracted_lines):
            continue
        line = extracted_lines[idx]
        if not first_line_taken:
            first_line_taken = True
            for key in _PARA_SPACING_KEYS:
                value = line.get(key)
                if value is not None:
                    spacing[key] = value
        position = Position(
            x0=line.get("x0", 0.0),
            y0=line.get("y0", 0.0),
            x1=line.get("x1", 0.0),
            y1=line.get("y1", 0.0),
        )
        font = Font(
            font_family=line.get("font_family", ""),
            font_size=line.get("font_size", 0.0),
            font_weight=line.get("font_weight", False),
            font_style=line.get("font_style", False),
            text=line.get("text", ""),
            line_no=line.get("line_no", 0),
        )
        elements.append(LineElement(position=position, font=font))

    if not elements and text:
        # 行索引未匹配到任何提取行：保留文本，位置/字号维持默认（未测得）。
        elements.append(LineElement(font=Font(text=text)))

    return Paragraph(
        elements=elements,
        outline_level=outline_level,
        alignment=alignment,
        **spacing,
    )


def _validate_header_slots(header: Header) -> None:
    """Swap classification_duration / issuing_number when they are misplaced.

    The classifier occasionally slots the header lines backwards (e.g. the
    secrecy line "密级▲长期" lands in issuing_number and the document number
    "X办〔2026〕号" in classification_duration).  Text features are
    unambiguous for these two slots, so a regex cross-check can safely swap
    them back.  Ambiguous cases (no clear feature on either side) are left
    untouched.
    """
    classification = header.classification_duration
    issuing = header.issuing_number
    if classification is None and issuing is None:
        return

    def _text(para: Paragraph | None) -> str:
        return "" if para is None else "".join(e.font.text for e in para.elements)

    def _looks_secrecy(text: str) -> bool:
        return bool(_SECRECY_TEXT_RE.search(text))

    def _looks_issuing_number(text: str) -> bool:
        return bool(_ISSUING_NUMBER_TEXT_RE.search(text))

    c_text = _text(classification)
    n_text = _text(issuing)
    misplaced = (c_text and _looks_issuing_number(c_text) and not _looks_secrecy(c_text)) or (
        n_text and _looks_secrecy(n_text) and not _looks_issuing_number(n_text)
    )
    if misplaced:
        logger.info("Header slot validation swapped classification_duration <-> issuing_number")
        header.classification_duration, header.issuing_number = issuing, classification


def _get_alignment_from_indices(
    line_indices: list[int],
    extracted_lines: list[dict[str, Any]],
) -> str:
    """Get alignment from the first matching extracted line."""
    for idx in line_indices:
        if 0 <= idx < len(extracted_lines):
            alignment = extracted_lines[idx].get("alignment", "")
            if alignment:
                return alignment
    return "left"


def _merge_body_text_into_paragraphs(
    body_main_text: list[tuple[str, list[int]]],
    extracted_lines: list[dict[str, Any]],
) -> list[tuple[str, list[int]]]:
    """Merge consecutive body_text/heading lines into paragraphs by Y-gap.

    Uses Y-coordinate gap analysis: lines with gaps exceeding
    1.5x the median line height are treated as paragraph boundaries.
    Heading lines are only merged with same-level headings.

    Args:
        body_main_text: List of (outline_level, [line_idx]) tuples.
        extracted_lines: Original extracted line data with position info.

    Returns:
        List of merged (outline_level, [idx1, idx2, ...]) tuples.
    """
    if len(body_main_text) <= 1:
        return body_main_text

    # Build sortable entries: (outline_level, line_idx, y0, y1)
    entries: list[tuple[str, int, float, float]] = []
    for outline, indices in body_main_text:
        idx = indices[0]
        if 0 <= idx < len(extracted_lines):
            line = extracted_lines[idx]
            y0 = line.get("y0", 0.0)
            y1 = line.get("y1", 0.0)
        else:
            y0, y1 = 0.0, 0.0
        entries.append((outline, idx, y0, y1))

    # DOCX short-circuit: python-docx provides no layout coordinates, so
    # every line's position is all-zero.  Each w:p is already a real
    # paragraph carrying its own measured spacing (space_before/after …);
    # merging exists for PDF/scanned visual line wrapping and would both
    # merge every consecutive same-outline Word paragraph into one (gap
    # is constantly 0) and swallow the middle paragraphs' spacing
    # (_build_paragraph takes spacing from the first line only).  Skip
    # merging so each Word paragraph stays its own Paragraph.
    if all(y0 == 0.0 and y1 == 0.0 for _, _, y0, y1 in entries):
        return body_main_text

    # Sort by Y coordinate
    entries.sort(key=lambda x: x[2])

    # Compute median line height for threshold.  (All-zero DOCX
    # positions already returned above; here at least one line has
    # real coordinates.)
    # NOTE: unpack y0/y1 per entry — writing `for _, _, _, y1` would
    # silently use the leaked loop variable y0 (the LAST entry's y0)
    # from the entries-building loop above, skewing the threshold.
    heights = [y1 - y0 for _, _, y0, y1 in entries if y1 > y0]
    median_height = sorted(heights)[len(heights) // 2] if heights else 20.0
    gap_threshold = max(median_height * 1.5, 5.0)

    merged: list[tuple[str, list[int]]] = []
    current_outline = entries[0][0]
    current_indices: list[int] = [entries[0][1]]

    for i in range(len(entries) - 1):
        _, curr_idx, _, curr_y1 = entries[i]
        next_outline, next_idx, next_y0, _ = entries[i + 1]

        gap = max(0.0, next_y0 - curr_y1)

        if current_outline != next_outline or gap > gap_threshold:
            merged.append((current_outline, list(current_indices)))
            current_outline = next_outline
            current_indices = [next_idx]
        else:
            current_indices.append(next_idx)

    merged.append((current_outline, list(current_indices)))
    return merged


# ---------------------------------------------------------------------------
# Rule engine → PageContent conversion
# ---------------------------------------------------------------------------


def _classified_lines_to_page_content(
    classified: list[ClassifiedLine],
    extracted_lines: list[dict[str, Any]],
    margin: Margin,
    warnings: list[str] | None = None,
) -> PageContent:
    """Convert rule engine classification results to PageContent model.

    Maps ClassifiedLine.field values to the corresponding Header/Body/Footer
    fields and builds Paragraph objects from the extracted line data.

    Lines the rule engine could not classify (field == "unclassified") are
    kept as body_text paragraphs in their original line order rather than
    dropped, and a summary entry is appended to *warnings* when a list is
    provided.
    """
    header_map: dict[str, list[int]] = {}
    body_title_indices: list[int] = []
    body_addressee_indices: list[int] = []
    body_main_text: list[tuple[str, list[int]]] = []  # (outline_level, indices)
    body_attachment_indices: list[int] = []
    body_signature_indices: list[int] = []
    body_date_indices: list[int] = []
    body_note_indices: list[int] = []
    footer_map: dict[str, list[int]] = {}
    footer_text_overrides: dict[str, str] = {}
    unclassified_count = 0

    for cl in classified:
        idx = cl.line_no
        field = cl.field

        if field in (
            "copy_number",
            "classification_duration",
            "urgency_level",
            "issuing_logo",
            "issuing_number",
            "signatory",
            "ruling_line_pos",
        ):
            header_map.setdefault(field, []).append(idx)

        elif field == "title":
            body_title_indices.append(idx)
        elif field == "addressee":
            body_addressee_indices.append(idx)
        elif field == "attachment_note":
            body_attachment_indices.append(idx)
        elif field == "issuing_signature":
            body_signature_indices.append(idx)
        elif field == "issue_date":
            body_date_indices.append(idx)
        elif field == "note":
            body_note_indices.append(idx)
        elif field.startswith("heading") or field == "body_text":
            outline = field if field.startswith("heading") else "body_text"
            body_main_text.append((outline, [idx]))

        elif field == "unclassified":
            # Fallback: never drop content — keep unclassified lines as
            # body text in their original line order.
            unclassified_count += 1
            body_main_text.append(("body_text", [idx]))

        elif field in (
            "carbon_copy",
            "closing_line",
            "issuing_office",
            "distribution_date",
            "page_number",
        ):
            footer_map.setdefault(field, []).append(idx)
            if field == "distribution_date":
                line_text = (
                    extracted_lines[idx].get("text", "") if 0 <= idx < len(extracted_lines) else ""
                )
                if cl.text != line_text:
                    footer_text_overrides["distribution_date"] = cl.text

    if unclassified_count:
        msg = f"{unclassified_count} 行未能自动归类，已按正文内容保留"
        logger.warning("Rule engine left %d line(s) unclassified", unclassified_count)
        if warnings is not None:
            warnings.append(msg)

    def _text_from_indices(indices: list[int]) -> str:
        parts = []
        for i in indices:
            if 0 <= i < len(extracted_lines):
                parts.append(extracted_lines[i].get("text", ""))
        return "".join(parts)

    # Build Header — slots without lines stay None (无此要素)
    header = Header(
        copy_number=_build_optional_para(header_map.get("copy_number", []), extracted_lines),
        classification_duration=_build_optional_para(
            header_map.get("classification_duration", []), extracted_lines
        ),
        urgency_level=_build_optional_para(header_map.get("urgency_level", []), extracted_lines),
        issuing_logo=_build_optional_para(header_map.get("issuing_logo", []), extracted_lines),
        issuing_number=_build_optional_para(header_map.get("issuing_number", []), extracted_lines),
        signatory=_build_optional_para(header_map.get("signatory", []), extracted_lines),
        ruling_line_pos=_build_optional_para(
            header_map.get("ruling_line_pos", []), extracted_lines
        ),
    )
    _validate_header_slots(header)

    # Merge consecutive body_text/heading lines into paragraphs
    body_main_text = _merge_body_text_into_paragraphs(
        body_main_text,
        extracted_lines,
    )

    # Build Body — slots without lines stay None (无此要素)
    main_text_paras = []
    for outline, indices in body_main_text:
        main_text_paras.append(
            _build_para_from_indices(indices, extracted_lines, outline_level=outline)
        )

    body = Body(
        title=_build_optional_para(body_title_indices, extracted_lines, "heading1"),
        addressee=_build_optional_para(body_addressee_indices, extracted_lines),
        main_text=main_text_paras,
        attachment_note=_build_optional_para(body_attachment_indices, extracted_lines),
        issuing_signature=_build_optional_para(body_signature_indices, extracted_lines),
        issue_date=_build_optional_para(body_date_indices, extracted_lines),
        note=_build_optional_para(body_note_indices, extracted_lines),
    )

    # Build Footer — slots without lines stay None (无此要素)
    footer = Footer(
        closing_line=_build_optional_para(footer_map.get("closing_line", []), extracted_lines),
        carbon_copy=_build_optional_para(footer_map.get("carbon_copy", []), extracted_lines),
        issuing_office=_build_optional_para(footer_map.get("issuing_office", []), extracted_lines),
        distribution_date=(
            _build_para_from_indices(
                footer_map["distribution_date"],
                extracted_lines,
                text_override=footer_text_overrides.get("distribution_date"),
            )
            if footer_map.get("distribution_date")
            else None
        ),
        page_number=_build_optional_para(footer_map.get("page_number", []), extracted_lines),
    )

    return PageContent(header=header, body=body, footer=footer, margin=margin)


def _sorted_tops_bottoms(para: Paragraph) -> tuple[list[float], list[float]]:
    """段落各行按 y0 排序后的 (tops, bottoms)。"""
    ys = sorted((e.position.y0, e.position.y1) for e in para.elements)
    return [y0 for y0, _ in ys], [y1 for _, y1 in ys]


def _intra_paragraph_gaps(para: Paragraph) -> list[float]:
    """段内相邻行的 bottom-to-top 行隙（≥0）；单行为空列表。"""
    tops, bottoms = _sorted_tops_bottoms(para)
    gaps = [tops[j + 1] - bottoms[j] for j in range(len(tops) - 1)]
    return [g for g in gaps if g >= 0]


def _region_paragraphs(page_content: PageContent) -> list[list[Paragraph]]:
    """按 header/body/footer 收集含元素的段落，区域内按首行 y0 排序。"""
    region_paragraphs: list[list[Paragraph]] = []
    for section in (page_content.header, page_content.body, page_content.footer):
        paragraphs: list[Paragraph] = []
        for _name, value in section:
            if isinstance(value, Paragraph) and value.elements:
                paragraphs.append(value)
            elif isinstance(value, list):
                paragraphs.extend(v for v in value if isinstance(v, Paragraph) and v.elements)
        # Reading order within the region: by first line's top coordinate.
        paragraphs.sort(key=lambda p: min(e.position.y0 for e in p.elements))
        region_paragraphs.append(paragraphs)
    return region_paragraphs


def _doc_typical_line_gap(page_contents: list[PageContent]) -> float | None:
    """文档级段内行隙中位数（pt）：跨页汇集所有多行段的段内行隙取上中位数。

    页级无多行段时（标题页/落款页常全是单行段），space_after 的行隙扣减
    回退到该值；全文都没有多行段时返回 None（调用方最终回退 0.0）。
    """
    gaps: list[float] = []
    for page_content in page_contents:
        for paragraphs in _region_paragraphs(page_content):
            for para in paragraphs:
                gaps.extend(_intra_paragraph_gaps(para))
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def _estimate_spacing_from_positions(
    page_content: PageContent,
    doc_typical_gap: float | None = None,
) -> None:
    """Estimate paragraph spacing from element positions (PDF pipeline, pt).

    - line_spacing: median of consecutive top-to-top (y0) differences
      between lines inside the paragraph;
    - space_after: raw bottom-to-top y-gap to the next paragraph **minus
      the normal intra-paragraph line gap** (the visual gap that a fixed
      line-spacing rule already produces: line_spacing - line_box_height).
      Without this subtraction a fully compliant document (fixed 28.95pt
      line spacing, zero paragraph spacing) would report ~8.95pt of bogus
      space_before/after. Negative results are truncated to 0.0.
    - space_before is always left None: the visual gap between two
      paragraphs cannot be attributed to one side or the other, and the
      same gap must not be booked twice (single-side accounting — the
      net gap lands only on the previous paragraph's space_after).
    - Estimation is per region (header/body/footer): the y-distance
      across a region boundary is page-layout whitespace (e.g. the whole
      footer area), not paragraph spacing, so no value is recorded there.

    The normal line gap of a paragraph is the median bottom-to-top gap
    between its own consecutive lines; single-line paragraphs use the
    page-level median of all intra-paragraph gaps. Fallback chain for the
    typical gap: page-level median → *doc_typical_gap* (document-level
    median over all pages, see _doc_typical_line_gap) → 0.0 (nothing to
    subtract — same as the raw-gap behavior).

    Positions come from PyMuPDF and are in pt. Anything that cannot be
    estimated (single-line paragraph, first/last paragraph of a region,
    degenerate ordering) is left as None — never a guessed 0.0.

    Args:
        page_content: The PageContent to update in place.
        doc_typical_gap: Document-level median intra-paragraph line gap
            (pt), used when this page has no multi-line paragraph.
    """
    region_paragraphs = _region_paragraphs(page_content)

    all_paragraphs = [p for paragraphs in region_paragraphs for p in paragraphs]
    if not all_paragraphs:
        return

    # Per-paragraph normal line gap: median bottom-to-top gap between
    # consecutive lines of the same paragraph. Page-level median of all
    # such gaps is the fallback for single-line paragraphs.
    normal_gap_by_para: dict[int, float] = {}
    page_gaps: list[float] = []
    for para in all_paragraphs:
        tops, _ = _sorted_tops_bottoms(para)
        if len(tops) < 2:
            continue
        diffs = sorted(tops[j + 1] - tops[j] for j in range(len(tops) - 1))
        median = diffs[len(diffs) // 2]
        if median > 0:
            para.line_spacing = round(median, 2)
        gaps = _intra_paragraph_gaps(para)
        if gaps:
            gaps.sort()
            normal_gap_by_para[id(para)] = gaps[len(gaps) // 2]
            page_gaps.extend(gaps)

    page_gaps.sort()
    if page_gaps:
        typical_gap = page_gaps[len(page_gaps) // 2]
    elif doc_typical_gap is not None:
        # 页内无多行段（标题页/落款页常如此）：回退文档级中位数，
        # 而不是 0.0（不扣行隙会抬高 space_after，跨页文档里误报段距）。
        typical_gap = doc_typical_gap
    else:
        typical_gap = 0.0

    # Inter-paragraph net spacing, per region, single-side accounting.
    for paragraphs in region_paragraphs:
        for i in range(len(paragraphs) - 1):
            curr, nxt = paragraphs[i], paragraphs[i + 1]
            _, curr_bottoms = _sorted_tops_bottoms(curr)
            next_tops, _ = _sorted_tops_bottoms(nxt)
            raw_gap = next_tops[0] - curr_bottoms[-1]
            if raw_gap < 0:
                continue  # degenerate ordering (e.g. side-by-side) — skip
            next_normal_gap = normal_gap_by_para.get(id(nxt), typical_gap)
            net_gap = max(0.0, raw_gap - next_normal_gap)
            curr.space_after = round(net_gap, 2)
            # nxt.space_before stays None — see docstring.


def _build_para_from_indices(
    indices: list[int],
    extracted_lines: list[dict[str, Any]],
    outline_level: str = "others",
    text_override: str | None = None,
) -> Paragraph:
    """Build a Paragraph from line indices in extracted_lines."""
    if not indices:
        return Paragraph(outline_level=outline_level)
    if text_override:
        # 先从原始行提取字体信息，再用 text_override 覆盖文本内容
        original = _build_paragraph(
            _text_from_indices_safe(indices, extracted_lines),
            indices,
            extracted_lines,
            outline_level,
        )
        new_elements = []
        for elem in original.elements:
            f = elem.font
            new_font = Font(
                font_family=f.font_family,
                font_size=f.font_size,
                font_weight=f.font_weight,
                font_style=f.font_style,
                text=text_override,
                line_no=f.line_no,
            )
            new_elements.append(LineElement(position=elem.position, font=new_font))
        return Paragraph(
            elements=new_elements,
            outline_level=outline_level,
            alignment=original.alignment,
            space_before=original.space_before,
            space_after=original.space_after,
            line_spacing=original.line_spacing,
            first_indent=original.first_indent,
            left_indent=original.left_indent,
            right_indent=original.right_indent,
        )
    return _build_paragraph(
        _text_from_indices_safe(indices, extracted_lines),
        indices,
        extracted_lines,
        outline_level,
    )


def _build_optional_para(
    indices: list[int],
    extracted_lines: list[dict[str, Any]],
    outline_level: str = "others",
) -> Paragraph | None:
    """Build an optional Paragraph from line indices."""
    if not indices:
        return None
    return _build_para_from_indices(indices, extracted_lines, outline_level)


def _text_from_indices_safe(
    indices: list[int],
    extracted_lines: list[dict[str, Any]],
) -> str:
    """Safely extract text from line indices."""
    parts = []
    for i in indices:
        if 0 <= i < len(extracted_lines):
            parts.append(extracted_lines[i].get("text", ""))
    return "".join(parts)
