"""Rule-based structure classification engine for GB/T 9704 documents.

Deterministic classifier that uses regex patterns, font metadata, alignment,
and position data to classify document lines into header/body/footer fields
with confidence scores. Replaces LLM calls for well-formatted documents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .page_regions import split_docx_regions, split_pdf_regions
from .rule_patterns import (
    ADDRESSEE_KEYWORDS,
    ADDRESSEE_SUFFIX,
    ATTACHMENT_NOTE_PATTERN,
    CARBON_COPY_PATTERN,
    CLASSIFICATION_PATTERN,
    CONFIDENCE_ALIGNMENT,
    CONFIDENCE_BASE,
    CONFIDENCE_FONT,
    CONFIDENCE_REGEX,
    CONFIDENCE_REGION,
    CONFIDENCE_SIGNAL_AGREEMENT,
    CONFIDENCE_STYLE_NAME,
    COPY_NUMBER_PATTERN,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DISTRIBUTION_DATE_PATTERN,
    FONT_SIZE_THRESHOLDS,
    HEADING_RULES,
    HEADING_RULES_OCR,
    ISSUE_DATE_PATTERN,
    ISSUE_DATE_PATTERN_OCR,
    ISSUING_LOGO_KEYWORDS,
    ISSUING_NUMBER_PATTERN,
    ISSUING_NUMBER_PATTERN_OCR,
    ISSUING_OFFICE_PATTERN,
    PAGE_NUMBER_PATTERN,
    SIGNATORY_PATTERN,
    STYLE_NAME_TO_FIELD,
    STYLE_NAME_TO_OUTLINE,
    TITLE_KEYWORDS,
    URGENCY_PATTERN,
)

logger = logging.getLogger(__name__)


@dataclass
class ClassifiedLine:
    """A line classified with a field assignment and confidence score."""

    line_no: int
    text: str
    field: str  # e.g. "title", "heading1", "body_text", "issuing_number"
    confidence: float
    line_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassifyResult:
    """Result of classifying a full page of lines."""

    lines: list[ClassifiedLine] = field(default_factory=list)
    header_lines: list[ClassifiedLine] = field(default_factory=list)
    body_lines: list[ClassifiedLine] = field(default_factory=list)
    footer_lines: list[ClassifiedLine] = field(default_factory=list)

    def all_confident(self, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> bool:
        """Check if all lines meet the confidence threshold."""
        return all(
            cl.confidence >= threshold
            for cl in self.lines
            if cl.field != "unclassified"
        )

    def low_confidence_lines(
        self, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    ) -> list[ClassifiedLine]:
        """Return lines below the confidence threshold."""
        return [
            cl for cl in self.lines
            if cl.confidence < threshold
        ]


class StructureRuleEngine:
    """Rule-based structure classifier for Chinese government documents.

    Classifies extracted text lines into GB/T 9704 document structure
    fields using deterministic rules based on:
    1. Regex pattern matching (numbering formats, keywords)
    2. Font metadata (family, size, weight)
    3. Alignment and position heuristics
    4. Style name (bonus signal when available)
    """

    def classify_lines(
        self,
        lines: list[dict[str, Any]],
        has_position: bool = False,
        page_height: float = 841.89,
    ) -> ClassifyResult:
        """Classify all lines on a page.

        Args:
            lines: Extracted text lines with text/font/position metadata.
            has_position: Whether lines have valid Y coordinates (PDF/scanned).
            page_height: Page height in points for region splitting.

        Returns:
            ClassifyResult with all lines classified and confidence scores.
        """
        if not lines:
            return ClassifyResult()

        # Step 1: Split into regions
        if has_position:
            header_group, body_group, footer_group = split_pdf_regions(
                lines, page_height
            )
        else:
            header_group, body_group, footer_group = split_docx_regions(lines)

        # Step 2: Classify each region
        header_cls = self._classify_header(header_group)
        body_cls = self._classify_body(body_group)
        footer_cls = self._classify_footer(footer_group)

        return ClassifyResult(
            lines=header_cls + body_cls + footer_cls,
            header_lines=header_cls,
            body_lines=body_cls,
            footer_lines=footer_cls,
        )

    def _classify_header(
        self, lines: list[dict[str, Any]]
    ) -> list[ClassifiedLine]:
        """Classify header region lines."""
        result: list[ClassifiedLine] = []

        for line in lines:
            text = line.get("text", "")
            style_name = line.get("style_name", "")
            style_field = STYLE_NAME_TO_FIELD.get(style_name, "")
            if style_field in ("issuing_logo", "issuing_number", "copy_number",
                               "classification_duration", "urgency_level", "signatory"):
                result.append(ClassifiedLine(
                    line_no=line.get("line_no", 0),
                    text=text,
                    field=style_field,
                    confidence=CONFIDENCE_BASE + CONFIDENCE_STYLE_NAME + CONFIDENCE_REGION,
                    line_data=line,
                ))
                continue
            classified = self._try_header_field(line, text)
            if classified:
                result.append(classified)
            else:
                result.append(ClassifiedLine(
                    line_no=line.get("line_no", 0),
                    text=text,
                    field="unclassified",
                    confidence=0.0,
                    line_data=line,
                ))

        return result

    def _try_header_field(
        self, line: dict[str, Any], text: str
    ) -> ClassifiedLine | None:
        """Try to classify a line as a specific header field."""
        # Base confidence includes region signal (we already know it's in header)
        confidence = CONFIDENCE_BASE + CONFIDENCE_REGION + CONFIDENCE_SIGNAL_AGREEMENT
        matched_field = ""

        # 份号
        if COPY_NUMBER_PATTERN.match(text):
            matched_field = "copy_number"
            confidence += CONFIDENCE_REGEX
            return self._build_classified(line, matched_field, confidence)

        # 密级
        if CLASSIFICATION_PATTERN.match(text):
            matched_field = "classification_duration"
            confidence += CONFIDENCE_REGEX
            return self._build_classified(line, matched_field, confidence)

        # 紧急程度
        if URGENCY_PATTERN.match(text):
            matched_field = "urgency_level"
            confidence += CONFIDENCE_REGEX
            return self._build_classified(line, matched_field, confidence)

        # 签发人
        if SIGNATORY_PATTERN.match(text):
            matched_field = "signatory"
            confidence += CONFIDENCE_REGEX
            return self._build_classified(line, matched_field, confidence)

        # 发文字号
        if self._match_pattern(text, ISSUING_NUMBER_PATTERN, ISSUING_NUMBER_PATTERN_OCR):
            matched_field = "issuing_number"
            confidence += CONFIDENCE_REGEX
            return self._build_classified(line, matched_field, confidence)

        # 发文机关标志 — large font + centered + keywords
        font_size = line.get("font_size", 0.0)
        alignment = line.get("alignment", "")
        font_family = line.get("font_family", "")

        if self._is_large_font(
            font_size,
            FONT_SIZE_THRESHOLDS["issuing_logo_min"],
            FONT_SIZE_THRESHOLDS["issuing_logo_min_scanned"],
            has_font_family=bool(font_family),
        ):
            logo_confidence = CONFIDENCE_BASE
            has_keyword = any(kw in text for kw in ISSUING_LOGO_KEYWORDS)
            is_centered = alignment == "center"
            is_heading_font = any(
                kw in font_family
                for kw in ("黑体", "方正小标宋", "宋体", "华文中宋")
            )

            if has_keyword:
                logo_confidence += CONFIDENCE_REGEX
            if is_centered:
                logo_confidence += CONFIDENCE_ALIGNMENT
            if is_heading_font or font_size >= 26.0:
                logo_confidence += CONFIDENCE_FONT

            if logo_confidence >= 0.6:
                return self._build_classified(
                    line, "issuing_logo", logo_confidence
                )

        return None

    def _classify_body(
        self, lines: list[dict[str, Any]]
    ) -> list[ClassifiedLine]:
        """Classify body region lines."""
        result: list[ClassifiedLine] = []

        for idx, line in enumerate(lines):
            text = line.get("text", "")
            style_name = line.get("style_name", "")
            style_field = STYLE_NAME_TO_FIELD.get(style_name, "")
            if style_field in ("title", "addressee", "issuing_signature",
                               "issue_date", "attachment_note"):
                result.append(ClassifiedLine(
                    line_no=line.get("line_no", 0),
                    text=text,
                    field=style_field,
                    confidence=CONFIDENCE_BASE + CONFIDENCE_STYLE_NAME + CONFIDENCE_REGION,
                    line_data=line,
                ))
                continue
            classified = self._try_body_special_field(line, text, idx, lines)
            if classified:
                result.append(classified)
            else:
                # Try outline level classification
                outline, conf = self._classify_outline_level(line)
                if outline:
                    result.append(ClassifiedLine(
                        line_no=line.get("line_no", 0),
                        text=text,
                        field=outline,
                        confidence=conf,
                        line_data=line,
                    ))
                else:
                    # Default body text — region already confirmed
                    result.append(ClassifiedLine(
                        line_no=line.get("line_no", 0),
                        text=text,
                        field="body_text",
                        confidence=CONFIDENCE_BASE + CONFIDENCE_REGION + 0.20,
                        line_data=line,
                    ))

        return result

    def _try_body_special_field(
        self,
        line: dict[str, Any],
        text: str,
        idx: int,
        all_lines: list[dict[str, Any]],
    ) -> ClassifiedLine | None:
        """Try to classify a body line as a specific field (not body_text)."""
        font_size = line.get("font_size", 0.0)
        alignment = line.get("alignment", "")
        font_family = line.get("font_family", "")

        # 标题 (title): large font + near top of body
        # Scanned PDFs lack font_family — be more lenient with threshold.
        is_large = self._is_large_font(
            font_size,
            FONT_SIZE_THRESHOLDS["title_min"],
            FONT_SIZE_THRESHOLDS["title_min_scanned"],
            has_font_family=bool(font_family),
        )
        title_threshold = 0.45 if not font_family else 0.6

        if idx == 0 and is_large:
            # Position at top of body is a strong title signal.
            title_conf = CONFIDENCE_BASE + CONFIDENCE_REGION + CONFIDENCE_SIGNAL_AGREEMENT
            if alignment == "center":
                title_conf += CONFIDENCE_ALIGNMENT
            if any(kw in font_family for kw in ("方正小标宋", "宋体", "黑体")):
                title_conf += CONFIDENCE_FONT
            if any(kw in text for kw in TITLE_KEYWORDS):
                title_conf += CONFIDENCE_REGEX
            if title_conf >= title_threshold:
                return self._build_classified(line, "title", title_conf)

        # Secondary path: large font near top, even if not idx==0.
        # For scanned PDFs, alignment is less reliable — accept "left" if
        # other signals (keywords) are present.
        if is_large and idx <= 2:
            title_conf = CONFIDENCE_BASE + CONFIDENCE_REGION
            if alignment == "center":
                title_conf += CONFIDENCE_ALIGNMENT
            if any(kw in text for kw in TITLE_KEYWORDS):
                title_conf += CONFIDENCE_REGEX
            if title_conf >= title_threshold:
                return self._build_classified(line, "title", title_conf)

        # 主送机关 (addressee): after title, ends with ：, contains org keywords
        if idx <= 3 and text.endswith(ADDRESSEE_SUFFIX):
            addr_conf = CONFIDENCE_BASE
            has_org_kw = any(kw in text for kw in ADDRESSEE_KEYWORDS)
            if has_org_kw:
                addr_conf += CONFIDENCE_REGEX
            if idx > 0 and self._is_likely_title_line(all_lines, idx - 1):
                addr_conf += CONFIDENCE_REGION
            if len(text) > 80:
                addr_conf -= 0.20
            if ("，" in text or "。" in text) and len(text) > 40:
                addr_conf -= 0.20
            if addr_conf >= 0.6:
                return self._build_classified(line, "addressee", addr_conf)

        # 附件说明 (attachment_note)
        if ATTACHMENT_NOTE_PATTERN.match(text):
            return self._build_classified(
                line,
                "attachment_note",
                CONFIDENCE_BASE + CONFIDENCE_REGEX,
            )

        # 成文日期 (issue_date): right-aligned, date format, near end
        if self._match_pattern(text, ISSUE_DATE_PATTERN, ISSUE_DATE_PATTERN_OCR):
            date_conf = CONFIDENCE_BASE + CONFIDENCE_REGEX
            if alignment == "right":
                date_conf += CONFIDENCE_ALIGNMENT
            if idx >= len(all_lines) - 4:
                date_conf += CONFIDENCE_REGION
            return self._build_classified(line, "issue_date", date_conf)

        # 发文机关署名 (issuing_signature): near end, not a date
        if (
            idx >= len(all_lines) - 5
            and not self._match_pattern(text, ISSUE_DATE_PATTERN, ISSUE_DATE_PATTERN_OCR)
            and not PAGE_NUMBER_PATTERN.match(text)
        ):
            if alignment in ("right", "center"):
                sig_conf = CONFIDENCE_BASE + CONFIDENCE_ALIGNMENT + CONFIDENCE_REGION
                return self._build_classified(line, "issuing_signature", sig_conf)
            elif not font_family:
                # Scanned PDF: alignment may be unreliable for short text
                if "。" not in text and "，" not in text and len(text) < 30:
                    sig_conf = CONFIDENCE_BASE + CONFIDENCE_REGION
                    return self._build_classified(line, "issuing_signature", sig_conf)

        # 附注 (note): starts with （, near end, typically short
        if text.startswith("（") and text.endswith("）") and idx >= len(all_lines) - 6:
            note_conf = CONFIDENCE_BASE + CONFIDENCE_REGEX
            return self._build_classified(line, "note", note_conf)

        return None

    def _classify_outline_level(
        self, line: dict[str, Any]
    ) -> tuple[str, float]:
        """Classify the outline level of a line.

        Returns:
            Tuple of (outline_level, confidence). Returns ("", 0.0) if
            no heading pattern matches.
        """
        text = line.get("text", "")
        font_family = line.get("font_family", "")
        font_weight = line.get("font_weight", False)
        style_name = line.get("style_name", "")

        style_outline = STYLE_NAME_TO_OUTLINE.get(style_name, "")

        # Check heading patterns (strict first, then OCR-resilient)
        for rules in (HEADING_RULES, HEADING_RULES_OCR):
            for outline_level, pattern, expected_font, expected_bold in rules:
                if pattern.match(text):
                    confidence = CONFIDENCE_BASE + CONFIDENCE_REGEX

                    # Accumulate penalties with cap to prevent over-elimination
                    penalties = 0.0
                    length_penalty = 0.15 if not font_family else 0.30
                    if len(text) > 50:
                        penalties += length_penalty
                    if "。" in text:
                        penalties += 0.50
                    if "，" in text and len(text) > 25:
                        penalties += 0.25
                    # Cap penalties so confidence never drops below the minimum
                    # threshold (CONFIDENCE_BASE + CONFIDENCE_REGION).
                    min_confidence = CONFIDENCE_BASE + CONFIDENCE_REGION
                    max_penalty = (CONFIDENCE_BASE + CONFIDENCE_REGEX) - min_confidence
                    penalties = min(penalties, max_penalty)
                    confidence -= penalties

                    if expected_font and expected_font in font_family:
                        if "。" not in text:
                            confidence += CONFIDENCE_FONT
                        else:
                            confidence += CONFIDENCE_FONT * 0.3
                    elif expected_bold and font_weight:
                        confidence += CONFIDENCE_FONT * 0.7

                    if style_outline == outline_level:
                        confidence = min(1.0, confidence + CONFIDENCE_STYLE_NAME)

                    if confidence < CONFIDENCE_BASE + CONFIDENCE_REGION:
                        continue

                    # 包含正文内容且缺乏标题支撑特征的长段落，降级为正文处理
                    if (
                        "。" in text
                        and len(text) > 30
                        and confidence < 0.55
                        and not font_weight
                    ):
                        continue

                    return outline_level, min(1.0, confidence)

        # If style_name indicates heading but no regex match
        if style_outline and style_outline.startswith("heading"):
            confidence = CONFIDENCE_BASE + CONFIDENCE_STYLE_NAME
            if font_weight:
                confidence += CONFIDENCE_FONT * 0.5
            return style_outline, min(1.0, confidence)

        return "", 0.0

    def _classify_footer(
        self, lines: list[dict[str, Any]]
    ) -> list[ClassifiedLine]:
        """Classify footer region lines."""
        result: list[ClassifiedLine] = []

        for line in lines:
            text = line.get("text", "")

            if text.startswith("（") and text.endswith("）"):
                result.append(ClassifiedLine(
                    line_no=line.get("line_no", 0),
                    text=text,
                    field="note",
                    confidence=CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION,
                    line_data=line,
                ))
                continue

            classified = self._try_footer_field(line, text)
            if classified:
                result.append(classified)
                if classified.field == "issuing_office":
                    date_match = DISTRIBUTION_DATE_PATTERN.search(text)
                    if date_match:
                        result.append(ClassifiedLine(
                            line_no=line.get("line_no", 0),
                            text=date_match.group(),
                            field="distribution_date",
                            confidence=CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION,
                            line_data=line,
                        ))
            else:
                result.append(ClassifiedLine(
                    line_no=line.get("line_no", 0),
                    text=text,
                    field="unclassified",
                    confidence=0.0,
                    line_data=line,
                ))

        return result

    def _try_footer_field(
        self, line: dict[str, Any], text: str
    ) -> ClassifiedLine | None:
        """Try to classify a line as a specific footer field."""
        # 抄送机关
        if CARBON_COPY_PATTERN.match(text):
            return self._build_classified(
                line,
                "carbon_copy",
                CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION,
            )

        # 页码
        if PAGE_NUMBER_PATTERN.match(text):
            return self._build_classified(
                line,
                "page_number",
                CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION,
            )

        # 印发机关
        if ISSUING_OFFICE_PATTERN.search(text):
            office_conf = CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION
            return self._build_classified(line, "issuing_office", office_conf)

        # 印发日期
        if DISTRIBUTION_DATE_PATTERN.search(text):
            date_conf = CONFIDENCE_BASE + CONFIDENCE_REGEX + CONFIDENCE_REGION
            return self._build_classified(
                line, "distribution_date", date_conf
            )

        return None

    @staticmethod
    def _is_large_font(
        font_size: float,
        absolute_min: float,
        scanned_min: float,
        has_font_family: bool = False,
    ) -> bool:
        if font_size >= absolute_min:
            return True
        if not has_font_family and font_size >= scanned_min:
            return True
        if font_size >= (
            FONT_SIZE_THRESHOLDS["large_relative_factor"]
            * FONT_SIZE_THRESHOLDS["body_text_standard"]
        ):
            return True
        return False

    @staticmethod
    def _match_pattern(text: str, strict: "re.Pattern[str]", ocr: "re.Pattern[str]") -> bool:
        return bool(strict.match(text) or ocr.match(text))

    def _build_classified(
        self,
        line: dict[str, Any],
        field: str,
        confidence: float,
    ) -> ClassifiedLine:
        """Build a ClassifiedLine from a raw line dict."""
        return ClassifiedLine(
            line_no=line.get("line_no", 0),
            text=line.get("text", ""),
            field=field,
            confidence=min(1.0, confidence),
            line_data=line,
        )

    def _is_likely_title_line(
        self, lines: list[dict[str, Any]], idx: int
    ) -> bool:
        if idx < 0 or idx >= len(lines):
            return False
        line = lines[idx]
        font_size = line.get("font_size", 0.0)
        font_family = line.get("font_family", "")
        return (
            self._is_large_font(
                font_size,
                FONT_SIZE_THRESHOLDS["title_min"],
                FONT_SIZE_THRESHOLDS["title_min_scanned"],
                has_font_family=bool(font_family),
            )
            and line.get("alignment", "") == "center"
        )
