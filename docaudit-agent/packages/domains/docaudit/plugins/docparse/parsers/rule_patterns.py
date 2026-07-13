"""Rule patterns and thresholds for GB/T 9704 document structure classification.

Defines compiled regex patterns, font size thresholds, and field matching
rules used by the StructureRuleEngine for deterministic classification
of Chinese government official documents.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# outline_level (标题层级) patterns
# ---------------------------------------------------------------------------

HEADING1_PATTERN = re.compile(r"^[一二三四五六七八九十]+、")
HEADING2_PATTERN = re.compile(r"^[（(][一二三四五六七八九十]+[）)]")
HEADING3_PATTERN = re.compile(r"^\d+[.．]")
HEADING4_PATTERN = re.compile(r"^[（(]\d+[）)]")

HEADING_RULES: list[tuple[str, re.Pattern[str], str, bool]] = [
    # (outline_level, compiled_pattern, expected_font_keyword, expected_bold)
    ("heading1", HEADING1_PATTERN, "黑体", True),
    ("heading2", HEADING2_PATTERN, "楷体", True),
    ("heading3", HEADING3_PATTERN, "仿宋", True),
    ("heading4", HEADING4_PATTERN, "仿宋", False),
]

# OCR-resilient heading variants: tolerate leading whitespace from OCR.
# These are hand-crafted — whitespace insertion patterns differ per heading level.
HEADING1_PATTERN_OCR = re.compile(r"^\s*[一二三四五六七八九十]+、")
HEADING2_PATTERN_OCR = re.compile(r"^\s*[（(]\s*[一二三四五六七八九十]+\s*[）)]")
HEADING3_PATTERN_OCR = re.compile(r"^\s*\d+\s*[.．]")
HEADING4_PATTERN_OCR = re.compile(r"^\s*[（(]\s*\d+\s*[）)]")

# Generate OCR heading rules from the standard rules so font/level config
# stays in one place — only the compiled pattern is swapped.
_OCR_PATTERN_MAP = {
    HEADING1_PATTERN: HEADING1_PATTERN_OCR,
    HEADING2_PATTERN: HEADING2_PATTERN_OCR,
    HEADING3_PATTERN: HEADING3_PATTERN_OCR,
    HEADING4_PATTERN: HEADING4_PATTERN_OCR,
}

HEADING_RULES_OCR: list[tuple[str, re.Pattern[str], str, bool]] = [
    (level, _OCR_PATTERN_MAP[pattern], font, bold)
    for level, pattern, font, bold in HEADING_RULES
]

# style_name → outline_level mapping
STYLE_NAME_TO_OUTLINE: dict[str, str] = {
    "Heading 1": "heading1",
    "Heading 2": "heading2",
    "Heading 3": "heading3",
    "Heading 4": "heading4",
    "Heading 5": "heading5",
    "标题 1": "heading1",
    "标题 2": "heading2",
    "标题 3": "heading3",
    "标题 4": "heading4",
    "标题 5": "heading5",
}

STYLE_NAME_TO_FIELD: dict[str, str] = {
    "HeaderLogo": "issuing_logo",
    "DocNumber": "issuing_number",
    "Title": "title",
    "Recipient": "addressee",
    "Signature": "issuing_signature",
    "Date": "issue_date",
}

# ---------------------------------------------------------------------------
# Header (版头) field patterns
# ---------------------------------------------------------------------------

COPY_NUMBER_PATTERN = re.compile(r"^\d{3,6}$")
CLASSIFICATION_PATTERN = re.compile(r"^(绝密|机密|秘密)★?\d*[个]?[年月日]*$")
URGENCY_PATTERN = re.compile(r"^(特急|加急|急件)$")
ISSUING_NUMBER_PATTERN = re.compile(r"^[^\s]+[（(〔\[]?\d{4}[）)〕\]]?\d*号$")
# OCR-resilient variant: tolerate extra whitespace around brackets
ISSUING_NUMBER_PATTERN_OCR = re.compile(
    r"^\s*[^\s]+[（(〔\[]\s*\d{4}\s*[）)〕\]]\s*\d*\s*号\s*$"
)
SIGNATORY_PATTERN = re.compile(r"^签发人[：:]")

ISSUING_LOGO_KEYWORDS: tuple[str, ...] = (
    "人民政府",
    "办公室",
    "委员会",
    "办公厅",
    "部",
    "厅",
    "局",
    "院",
    "署",
    "处",
)

# ---------------------------------------------------------------------------
# Body (主体) field patterns
# ---------------------------------------------------------------------------

ATTACHMENT_NOTE_PATTERN = re.compile(r"^附件[：:]")
ISSUE_DATE_PATTERN = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")
# OCR-resilient variant: tolerate leading/trailing whitespace
ISSUE_DATE_PATTERN_OCR = re.compile(r"^\s*\d{4}年\d{1,2}月\d{1,2}日\s*$")
TITLE_KEYWORDS: tuple[str, ...] = (
    "关于",
    "通知",
    "决定",
    "意见",
    "办法",
    "规定",
    "方案",
    "报告",
    "请示",
    "批复",
    "函",
    "纪要",
    "命令",
    "通告",
    "通报",
    "公告",
    "标准",
    "规范",
    "细则",
    "条例",
    "规则",
    "指示",
    "决议",
    "工作",
    "文件",
)

ADDRESSEE_SUFFIX = ("：", "；", ":")
ADDRESSEE_KEYWORDS: tuple[str, ...] = (
    "局",
    "部",
    "厅",
    "委",
    "办",
    "院",
    "会",
    "处",
    "科",
    "股",
    "室",
    "中心",
    "总站",
    "各",
)

# ---------------------------------------------------------------------------
# Footer (版记) field patterns
# ---------------------------------------------------------------------------

CARBON_COPY_PATTERN = re.compile(r"^抄送[：:]")
ISSUING_OFFICE_PATTERN = re.compile(r"印发|印刷")
DISTRIBUTION_DATE_PATTERN = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
PAGE_NUMBER_PATTERN = re.compile(r"^[-—]?\s*\d+\s*[-—]?$|^第\d+页$|^-\s*\d+\s*-$")

FOOTER_KEYWORDS: tuple[str, ...] = ("抄送", "印发", "印刷", "翻印")

# ---------------------------------------------------------------------------
# Font size thresholds (GB/T 9704 standard, in points)
# ---------------------------------------------------------------------------

FONT_SIZE_THRESHOLDS = {
    "title_min": 22.0,
    "title_min_scanned": 18.0,
    "issuing_logo_min": 22.0,
    "issuing_logo_min_scanned": 18.0,
    "body_text_standard": 16.0,
    "sub_heading_indicator": 16.0,
    "large_relative_factor": 1.25,
}

# font_family substring → semantic mapping
FONT_FAMILY_SEMANTICS: dict[str, str] = {
    "黑体": "heading",
    "楷体": "sub_heading",
    "楷体_GB2312": "sub_heading",
    "仿宋": "body_text",
    "仿宋_GB2312": "body_text",
    "宋体": "body_text",
    "方正小标宋": "title",
    "等线": "body_text",
}

# ---------------------------------------------------------------------------
# Confidence score contributions
# ---------------------------------------------------------------------------

CONFIDENCE_BASE = 0.20
CONFIDENCE_REGEX = 0.40
CONFIDENCE_FONT = 0.30
CONFIDENCE_ALIGNMENT = 0.10
CONFIDENCE_REGION = 0.10
CONFIDENCE_STYLE_NAME = 0.10
CONFIDENCE_BLOCK_LABEL = 0.20
CONFIDENCE_SIGNAL_AGREEMENT = 0.15

# Minimum confidence to skip LLM fallback
DEFAULT_CONFIDENCE_THRESHOLD = 0.80
