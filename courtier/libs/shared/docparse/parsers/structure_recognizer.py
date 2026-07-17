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
    Margin,
    MetaData,
    PageContent,
    Paragraph,
    Position,
)

from .llm_client import LLMClient
from .rules import ClassifiedLine

if TYPE_CHECKING:
    from .base import ParserConfig

logger = logging.getLogger(__name__)


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

    elements: list[MetaData] = []
    for idx in line_indices:
        if idx < 0 or idx >= len(extracted_lines):
            continue
        line = extracted_lines[idx]
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
        elements.append(MetaData(exist=True, position=position, font=font))

    if not elements and text:
        element = MetaData(
            exist=False,
            position=Position(),
            font=Font(text=text),
        )
        elements.append(element)

    return Paragraph(
        elements=elements, outline_level=outline_level, alignment=alignment
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
    return Paragraph(elements=[MetaData(exist=True, position=pos, font=Font())])


def _build_ruling_line(
    data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Paragraph:
    """Build ruling_line_pos paragraph, preferring reference_line_index."""
    if data is None:
        return Paragraph()
    ref_idx = data.get("reference_line_index")
    if isinstance(ref_idx, (int, float)) and not isinstance(ref_idx, bool):
        ref_idx = int(ref_idx)
        if 0 <= ref_idx < len(extracted_lines):
            return _build_line_paragraph(ref_idx, extracted_lines, "after")
    return _build_paragraph(
        _safe_get(data, "text", ""),
        _safe_get(data, "line_indices", []),
        extracted_lines,
    )


def _build_closing_line(
    data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Paragraph:
    """Build closing_line paragraph, preferring reference_line_index."""
    if data is None:
        return Paragraph()
    ref_idx = data.get("reference_line_index")
    if isinstance(ref_idx, (int, float)) and not isinstance(ref_idx, bool):
        ref_idx = int(ref_idx)
        if 0 <= ref_idx < len(extracted_lines):
            return _build_line_paragraph(ref_idx, extracted_lines, "before")
    return _build_paragraph(
        _safe_get(data, "text", ""),
        _safe_get(data, "line_indices", []),
        extracted_lines,
    )


def _parse_header(
    header_data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
) -> Header:
    """Parse LLM header classification into Header model."""
    if not header_data:
        return Header()

    return Header(
        copy_number=_build_optional_paragraph(
            header_data.get("copy_number"), extracted_lines
        ),
        classification_duration=_build_optional_paragraph(
            header_data.get("classification_duration"), extracted_lines
        ),
        urgency_level=_build_optional_paragraph(
            header_data.get("urgency_level"), extracted_lines
        ),
        issuing_logo=_build_paragraph(
            _safe_get(header_data, "issuing_logo", {}).get("text", ""),
            _safe_get(header_data, "issuing_logo", {}).get("line_indices", []),
            extracted_lines,
        ),
        issuing_number=_build_paragraph(
            _safe_get(header_data, "issuing_number", {}).get("text", ""),
            _safe_get(header_data, "issuing_number", {}).get("line_indices", []),
            extracted_lines,
        ),
        signatory=_build_paragraph(
            _safe_get(header_data, "signatory", {}).get("text", ""),
            _safe_get(header_data, "signatory", {}).get("line_indices", []),
            extracted_lines,
        ),
        ruling_line_pos=_build_ruling_line(
            header_data.get("ruling_line_pos"),
            extracted_lines,
        ),
    )


def _parse_body(
    body_data: dict[str, Any] | None,
    extracted_lines: list[dict[str, Any]],
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
                # Post-process: heading1/2/3 must be bold; downgrade if not
                if outline in ("heading1", "heading2", "heading3"):
                    has_bold = any(
                        extracted_lines[idx].get("font_weight", False)
                        for idx in line_indices
                        if isinstance(idx, int) and 0 <= idx < len(extracted_lines)
                    )
                    if not has_bold:
                        outline = "body_text"

                para = _build_paragraph(
                    para_data.get("text", ""),
                    line_indices,
                    extracted_lines,
                    outline_level=outline,
                )
                main_text.append(para)

    return Body(
        title=_build_paragraph(
            _safe_get(body_data, "title", {}).get("text", ""),
            _safe_get(body_data, "title", {}).get("line_indices", []),
            extracted_lines,
            outline_level="heading1",
        ),
        addressee=_build_paragraph(
            _safe_get(body_data, "addressee", {}).get("text", ""),
            _safe_get(body_data, "addressee", {}).get("line_indices", []),
            extracted_lines,
        ),
        main_text=main_text,
        attachment_note=_build_attachment_note(
            body_data.get("attachment_note"), extracted_lines
        ),
        issuing_signature=_build_paragraph(
            _safe_get(body_data, "issuing_signature", {}).get("text", ""),
            _safe_get(body_data, "issuing_signature", {}).get("line_indices", []),
            extracted_lines,
        ),
        issue_date=_build_paragraph(
            _safe_get(body_data, "issue_date", {}).get("text", ""),
            _safe_get(body_data, "issue_date", {}).get("line_indices", []),
            extracted_lines,
        ),
        stamp=_build_optional_paragraph(body_data.get("stamp"), extracted_lines),
        note=_build_optional_paragraph(body_data.get("note"), extracted_lines),
        attachments=_build_optional_paragraph(
            body_data.get("attachments"), extracted_lines
        ),
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
        carbon_copy=_build_optional_paragraph(
            footer_data.get("carbon_copy"), extracted_lines
        ),
        issuing_office=_build_paragraph(
            _safe_get(footer_data, "issuing_office", {}).get("text", ""),
            _safe_get(footer_data, "issuing_office", {}).get("line_indices", []),
            extracted_lines,
        ),
        distribution_date=_build_paragraph(
            _safe_get(footer_data, "distribution_date", {}).get("text", ""),
            _safe_get(footer_data, "distribution_date", {}).get("line_indices", []),
            extracted_lines,
        ),
        page_number=_build_paragraph(
            _safe_get(footer_data, "page_number", {}).get("text", ""),
            _safe_get(footer_data, "page_number", {}).get("line_indices", []),
            extracted_lines,
        ),
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
        return _build_paragraph(
            combined_text, combined_indices, extracted_lines, outline_level
        )
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
) -> Paragraph | None:
    """Build attachment_note paragraph, splitting items by paragraph breaks.

    When multiple attachment items appear on separate lines with different
    formatting (e.g. x0 positions), this inserts line-break separation
    so they render as distinct items in the output document.
    """
    para = _build_optional_paragraph(data, extracted_lines)
    if para is None or len(para.elements) < 2:
        return para

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

        # Use the same 40pt threshold as spacing.py for x0-based breaks.
        # Estimate scale: assume ~595pt A4 width, ~1280px typical image.
        # But we don't have img_width here, so use a generous pixel
        # threshold derived from 40pt at ~2.15 px/pt.
        is_break = y_gap_px > 30.0 or x0_diff_px > 85.0  # 85px ≈ 40pt

        if is_break:
            nxt.font = nxt.font.model_copy(
                update={"text": "\n" + nxt.font.text}
            )

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

    # Sort by Y coordinate
    entries.sort(key=lambda x: x[2])

    # Compute median line height for threshold.
    # NOTE: When all y0 values are 0.0 (e.g. DOCX without position data),
    # heights will be empty and median_height falls back to 20.0.
    heights = [y1 - y0 for _, _, _, y1 in entries if y1 > y0]
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
) -> PageContent:
    """Convert rule engine classification results to PageContent model.

    Maps ClassifiedLine.field values to the corresponding Header/Body/Footer
    fields and builds Paragraph objects from the extracted line data.
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
                    extracted_lines[idx].get("text", "")
                    if 0 <= idx < len(extracted_lines)
                    else ""
                )
                if cl.text != line_text:
                    footer_text_overrides["distribution_date"] = cl.text

    def _text_from_indices(indices: list[int]) -> str:
        parts = []
        for i in indices:
            if 0 <= i < len(extracted_lines):
                parts.append(extracted_lines[i].get("text", ""))
        return "".join(parts)

    # Build Header
    header = Header(
        copy_number=_build_optional_para(
            header_map.get("copy_number", []), extracted_lines
        ),
        classification_duration=_build_optional_para(
            header_map.get("classification_duration", []), extracted_lines
        ),
        urgency_level=_build_optional_para(
            header_map.get("urgency_level", []), extracted_lines
        ),
        issuing_logo=_build_para_from_indices(
            header_map.get("issuing_logo", []), extracted_lines
        ),
        issuing_number=_build_para_from_indices(
            header_map.get("issuing_number", []), extracted_lines
        ),
        signatory=_build_para_from_indices(
            header_map.get("signatory", []), extracted_lines
        ),
        ruling_line_pos=_build_para_from_indices(
            header_map.get("ruling_line_pos", []), extracted_lines
        ),
    )

    # Merge consecutive body_text/heading lines into paragraphs
    body_main_text = _merge_body_text_into_paragraphs(
        body_main_text,
        extracted_lines,
    )

    # Build Body
    main_text_paras = []
    for outline, indices in body_main_text:
        main_text_paras.append(
            _build_para_from_indices(indices, extracted_lines, outline_level=outline)
        )

    body = Body(
        title=_build_para_from_indices(body_title_indices, extracted_lines, "heading1"),
        addressee=_build_para_from_indices(body_addressee_indices, extracted_lines),
        main_text=main_text_paras,
        attachment_note=_build_optional_para(body_attachment_indices, extracted_lines),
        issuing_signature=_build_para_from_indices(
            body_signature_indices, extracted_lines
        ),
        issue_date=_build_para_from_indices(body_date_indices, extracted_lines),
        note=_build_optional_para(body_note_indices, extracted_lines),
    )

    # Build Footer
    footer = Footer(
        closing_line=_build_para_from_indices(
            footer_map.get("closing_line", []), extracted_lines
        ),
        carbon_copy=_build_optional_para(
            footer_map.get("carbon_copy", []), extracted_lines
        ),
        issuing_office=_build_para_from_indices(
            footer_map.get("issuing_office", []), extracted_lines
        ),
        distribution_date=_build_para_from_indices(
            footer_map.get("distribution_date", []),
            extracted_lines,
            text_override=footer_text_overrides.get("distribution_date"),
        ),
        page_number=_build_para_from_indices(
            footer_map.get("page_number", []), extracted_lines
        ),
    )

    return PageContent(header=header, body=body, footer=footer, margin=margin)


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
            new_elements.append(
                MetaData(exist=elem.exist, position=elem.position, font=new_font)
            )
        return Paragraph(
            elements=new_elements,
            outline_level=outline_level,
            alignment=original.alignment,
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
) -> PageContent:
    """Classify page structure using LLM.

    Args:
        extracted_lines: Lines extracted from the document with
            position/font info.
        margin: Page margin information.
        llm_client: Multimodal LLM client for structure recognition.
        image_path: Path to the document page image.
        config: Parser configuration.

    Returns:
        PageContent with header, body, footer populated.
    """
    if not extracted_lines:
        return PageContent(margin=margin)

    structure = llm_client.recognize_structure(extracted_lines, image_path)

    header = _parse_header(structure.get("header"), extracted_lines)
    body = _parse_body(structure.get("body"), extracted_lines)
    footer = _parse_footer(structure.get("footer"), extracted_lines)

    return PageContent(header=header, body=body, footer=footer, margin=margin)


def recognize_page_structure_text_only(
    extracted_lines: list[dict[str, Any]],
    margin: Margin,
    llm_client: LLMClient,
    config: ParserConfig | None = None,
) -> PageContent:
    """Classify page structure using text-only LLM (no image).

    Used by DOCX parser when rule engine has low confidence but
    we don't want to invoke LibreOffice for image generation.

    Args:
        extracted_lines: Lines extracted from the document.
        margin: Page margin information.
        llm_client: LLM client instance.
        config: Parser configuration.

    Returns:
        PageContent with header, body, footer populated.
    """
    if not extracted_lines:
        return PageContent(margin=margin)

    structure = llm_client.recognize_structure_text_only(extracted_lines)

    header = _parse_header(structure.get("header"), extracted_lines)
    body = _parse_body(structure.get("body"), extracted_lines)
    footer = _parse_footer(structure.get("footer"), extracted_lines)

    return PageContent(header=header, body=body, footer=footer, margin=margin)
