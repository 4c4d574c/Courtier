"""Structure recognizer: LLM-based page structure classification for scanned documents.

Provides the main `recognize_page_structure()` entry point that uses
multimodal LLM to classify lines into header/body/footer structure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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

from .llm_client import LLMClient
from .rules import ClassifiedLine

if TYPE_CHECKING:
    from .base import ParserConfig

logger = logging.getLogger(__name__)

# Reference geometry for _build_attachment_note break detection: the
# thresholds below were tuned on ~1280px-wide page images (85px ≈ 40pt
# at A4 width) and are scaled by actual image width at call time.
_ATTACHMENT_REF_IMG_WIDTH_PX = 1280.0
_ATTACHMENT_Y_GAP_REF_PX = 30.0
_ATTACHMENT_X0_BREAK_REF_PX = 85.0

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

# GB/T 9704 各级标题的期望字体族：一级黑体、二级楷体、三级仿宋。
# 用于对 LLM 判出的标题做字体族矛盾校验（见 _parse_body）。
_HEADING_EXPECTED_FAMILY: dict[str, set[str]] = {
    "heading1": {"黑体"},
    "heading2": {"楷体"},
    "heading3": {"仿宋"},
}


def _safe_get(data: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """Safely get a value from a dict that may contain None values.

    Unlike dict.get(), this also handles the case where the key exists
    but its value is None — returning the default instead.
    """
    if data is None:
        return default
    value = data.get(key, default)
    return default if value is None else value


def _build_paragraph(
    text: str,
    line_indices: list[int],
    extracted_lines: list[dict[str, Any]],
    outline_level: str = "others",
) -> Paragraph:
    """Build a Paragraph from LLM classification and extracted line data.

    Args:
        text: The text content from LLM classification.
        line_indices: Line indices from LLM classification.
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


def _build_line_paragraph(
    reference_line_index: int,
    extracted_lines: list[dict[str, Any]],
    position: str,
) -> Paragraph:
    """Build a Paragraph for a visual line (ruling_line / closing_line).

    Since these are graphic lines (not text), OCR does not detect them.  We
    derive their position from the adjacent text line that the LLM points to
    via *reference_line_index*.

    Args:
        reference_line_index: Index of the adjacent text line.
        extracted_lines: Original extracted line data.
        position: "after" — the line sits just below the reference line
                  (red ruling line after the last header element).
                  "before" — the line sits just above the reference line
                  (black closing line before the first footer element).
    """
    if reference_line_index < 0 or reference_line_index >= len(extracted_lines):
        return Paragraph()

    ref = extracted_lines[reference_line_index]
    ref_y0 = ref.get("y0", 0.0)
    ref_y1 = ref.get("y1", 0.0)
    ref_x0 = ref.get("x0", 0.0)
    ref_x1 = ref.get("x1", 0.0)
    line_height = max(ref_y1 - ref_y0, 1.0)
    gap = line_height * 0.6

    if position == "after":
        y0 = ref_y1 + gap
    else:
        y0 = ref_y0 - gap - line_height

    y1 = y0 + line_height

    pos = Position(x0=ref_x0, y0=y0, x1=ref_x1, y1=y1)
    return Paragraph(elements=[LineElement(position=pos, font=Font())])


def _build_ruling_line(
    data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Paragraph | None:
    """Build ruling_line_pos paragraph, preferring reference_line_index."""
    if data is None:
        return None
    ref_idx = data.get("reference_line_index")
    if isinstance(ref_idx, (int, float)) and not isinstance(ref_idx, bool):
        ref_idx = int(ref_idx)
        if 0 <= ref_idx < len(extracted_lines):
            return _build_line_paragraph(ref_idx, extracted_lines, "after")
    return _build_optional_paragraph(
        {"text": _safe_get(data, "text", ""), "line_indices": _safe_get(data, "line_indices", [])},
        extracted_lines,
    )


def _build_closing_line(
    data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Paragraph | None:
    """Build closing_line paragraph, preferring reference_line_index."""
    if data is None:
        return None
    ref_idx = data.get("reference_line_index")
    if isinstance(ref_idx, (int, float)) and not isinstance(ref_idx, bool):
        ref_idx = int(ref_idx)
        if 0 <= ref_idx < len(extracted_lines):
            return _build_line_paragraph(ref_idx, extracted_lines, "before")
    return _build_optional_paragraph(
        {"text": _safe_get(data, "text", ""), "line_indices": _safe_get(data, "line_indices", [])},
        extracted_lines,
    )


def _parse_header(
    header_data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Header:
    """Parse LLM header classification into Header model.

    Slots the LLM did not identify stay None (无此要素), not empty
    Paragraph skeletons.
    """
    if not header_data:
        return Header()

    return Header(
        copy_number=_build_optional_paragraph(header_data.get("copy_number"), extracted_lines),
        classification_duration=_build_optional_paragraph(
            header_data.get("classification_duration"), extracted_lines
        ),
        urgency_level=_build_optional_paragraph(header_data.get("urgency_level"), extracted_lines),
        issuing_logo=_build_optional_paragraph(header_data.get("issuing_logo"), extracted_lines),
        issuing_number=_build_optional_paragraph(
            header_data.get("issuing_number"), extracted_lines
        ),
        signatory=_build_optional_paragraph(header_data.get("signatory"), extracted_lines),
        ruling_line_pos=_build_ruling_line(
            header_data.get("ruling_line_pos"),
            extracted_lines,
        ),
    )


def _parse_body(
    body_data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
    img_width: float = 0.0,
) -> Body:
    """Parse LLM body classification into Body model."""
    if not body_data:
        return Body()

    main_text_data = body_data.get("main_text", [])
    main_text: list[Paragraph] = []
    if isinstance(main_text_data, list):
        for para_data in main_text_data:
            if isinstance(para_data, dict):
                outline = para_data.get("outline_level", "body_text")
                if outline not in (
                    "heading1",
                    "heading2",
                    "heading3",
                    "heading4",
                    "heading5",
                    "body_text",
                    "others",
                ):
                    outline = "body_text"

                line_indices = para_data.get("line_indices", [])
                # 字体族矛盾校验：LLM 判出的 heading1/2/3，若命中行的实测
                # 字体族与期望族全部冲突，降为 body_text；有匹配则保留。
                # 无实测字体（如字体 LLM 识别失败）时信任 LLM 分类。
                # 注意：加粗不再作为标题判定依据——黑体/楷体的"粗"来自
                # 字形本身，GB/T 9704 并无加粗要求。heading4/5 不校验。
                if outline in _HEADING_EXPECTED_FAMILY:
                    measured = {
                        extracted_lines[idx].get("font_family") or ""
                        for idx in line_indices
                        if isinstance(idx, int) and 0 <= idx < len(extracted_lines)
                    }
                    measured.discard("")
                    if measured and not measured & _HEADING_EXPECTED_FAMILY[outline]:
                        outline = "body_text"

                para = _build_paragraph(
                    para_data.get("text", ""),
                    line_indices,
                    extracted_lines,
                    outline_level=outline,
                )
                main_text.append(para)

    return Body(
        title=_build_optional_paragraph(
            body_data.get("title"), extracted_lines, outline_level="heading1"
        ),
        addressee=_build_optional_paragraph(body_data.get("addressee"), extracted_lines),
        main_text=main_text,
        attachment_note=_build_attachment_note(
            body_data.get("attachment_note"), extracted_lines, img_width
        ),
        issuing_signature=_build_optional_paragraph(
            body_data.get("issuing_signature"), extracted_lines
        ),
        issue_date=_build_optional_paragraph(body_data.get("issue_date"), extracted_lines),
        stamp=_build_optional_paragraph(body_data.get("stamp"), extracted_lines),
        note=_build_optional_paragraph(body_data.get("note"), extracted_lines),
        attachments=_build_optional_paragraph(body_data.get("attachments"), extracted_lines),
    )


def _parse_footer(
    footer_data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Footer:
    """Parse LLM footer classification into Footer model."""
    if not footer_data:
        return Footer()

    return Footer(
        closing_line=_build_closing_line(
            footer_data.get("closing_line"),
            extracted_lines,
        ),
        carbon_copy=_build_optional_paragraph(footer_data.get("carbon_copy"), extracted_lines),
        issuing_office=_build_optional_paragraph(
            footer_data.get("issuing_office"), extracted_lines
        ),
        distribution_date=_build_optional_paragraph(
            footer_data.get("distribution_date"), extracted_lines
        ),
        page_number=_build_optional_paragraph(footer_data.get("page_number"), extracted_lines),
    )


def _build_optional_paragraph(
    data: dict[str, Any] | list[Any] | None,
    extracted_lines: list[dict[str, Any]],
    outline_level: str = "others",
) -> Paragraph | None:
    """Build an optional Paragraph (returns None if data is None or empty).

    Handles both dict (single item) and list (multiple items from LLM).
    """
    if data is None:
        return None
    # LLM sometimes returns a list of items for fields like "attachments"
    if isinstance(data, list):
        combined_text = ""
        combined_indices: list[int] = []
        for item in data:
            if isinstance(item, dict):
                combined_text += item.get("text", "")
                combined_indices.extend(item.get("line_indices", []))
        if not combined_text and not combined_indices:
            return None
        return _build_paragraph(combined_text, combined_indices, extracted_lines, outline_level)
    if isinstance(data, dict):
        text = data.get("text", "")
        line_indices = data.get("line_indices", [])
        if not text and not line_indices:
            return None
        return _build_paragraph(text, line_indices, extracted_lines, outline_level)
    return None


def _build_attachment_note(
    data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
    img_width: float = 0.0,
) -> Paragraph | None:
    """Build attachment_note paragraph, splitting items by paragraph breaks.

    When multiple attachment items appear on separate lines with different
    formatting (e.g. x0 positions), this inserts line-break separation
    so they render as distinct items in the output document.

    Break-detection thresholds were tuned on ~1280px-wide page images
    (85px ≈ 40pt at A4 width) and are scaled proportionally to the
    actual image width, so high-DPI scans (e.g. 2480px at 300 DPI)
    behave the same as lower-resolution ones.
    """
    para = _build_optional_paragraph(data, extracted_lines)
    if para is None or len(para.elements) < 2:
        return para

    scale = img_width / _ATTACHMENT_REF_IMG_WIDTH_PX if img_width > 0 else 1.0
    y_gap_threshold = _ATTACHMENT_Y_GAP_REF_PX * scale
    x0_break_threshold = _ATTACHMENT_X0_BREAK_REF_PX * scale

    # Detect paragraph breaks between consecutive elements using Y-gap
    # and x0-position changes.  When a break is found, prefix the second
    # element's text with a newline so it renders as a separate line.
    for i in range(len(para.elements) - 1):
        curr = para.elements[i]
        nxt = para.elements[i + 1]

        # Y-gap between bottom of current and top of next
        y_gap_px = nxt.position.y0 - curr.position.y1

        # x0 position change
        x0_diff_px = abs(nxt.position.x0 - curr.position.x0)

        # Use the same 40pt threshold as spacing.py for x0-based breaks,
        # scaled from the reference image width to the actual width.
        is_break = y_gap_px > y_gap_threshold or x0_diff_px > x0_break_threshold

        if is_break:
            nxt.font = nxt.font.model_copy(update={"text": "\n" + nxt.font.text})

    return para


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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def recognize_page_structure(
    extracted_lines: list[dict[str, Any]],
    margin: Margin,
    llm_client: LLMClient,
    image_path: str,
    config: ParserConfig | None = None,
    img_width: float = 0.0,
) -> PageContent:
    """Classify page structure using LLM.

    Args:
        extracted_lines: Lines extracted from the document with
            position/font info.
        margin: Page margin information.
        llm_client: Multimodal LLM client for structure recognition.
        image_path: Path to the document page image.
        config: Parser configuration.
        img_width: Page image width in pixels, used to scale
            geometry thresholds (e.g. attachment_note break detection).
            0 means unknown — reference-width thresholds are used as-is.

    Returns:
        PageContent with header, body, footer populated.
    """
    if not extracted_lines:
        return PageContent(margin=margin)

    structure = llm_client.recognize_structure(extracted_lines, image_path)

    header = _parse_header(structure.get("header"), extracted_lines)
    body = _parse_body(structure.get("body"), extracted_lines, img_width)
    footer = _parse_footer(structure.get("footer"), extracted_lines)

    return PageContent(header=header, body=body, footer=footer, margin=margin)
