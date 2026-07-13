"""Internal shared utilities for dbop subpackage."""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

_DEFAULT_DB_URL = os.getenv("MYSQL_URL", "")


def _ensure_db_url(db_url: str) -> None:
    """Validate that a DB URL is non-empty before constructing AsyncDatabase."""
    if not db_url or not db_url.strip():
        raise ValueError(
            "Database URL is empty or not configured. "
            "Set the MYSQL_URL environment variable or pass db_url explicitly."
        )


# Shared section-type → PageContent attribute mapping.
# Each entry: (section_type, section_attr, field_name, is_optional, is_list)
# Used by both save_doc and load_doc to avoid duplicating the section-type enumeration.
_SECTION_ENTRY = tuple[str, str, str, bool, bool]

SECTION_FIELDS: list[_SECTION_ENTRY] = [
    # Header section
    ("header_copy_number", "header", "copy_number", True, False),
    ("header_classification_duration", "header", "classification_duration", True, False),
    ("header_urgency_level", "header", "urgency_level", True, False),
    ("header_issuing_logo", "header", "issuing_logo", False, False),
    ("header_issuing_number", "header", "issuing_number", False, False),
    ("header_signatory", "header", "signatory", False, False),
    ("header_ruling_line_pos", "header", "ruling_line_pos", False, False),
    # Body section
    ("body_title", "body", "title", False, False),
    ("body_addressee", "body", "addressee", False, False),
    ("body_main_text", "body", "main_text", False, True),
    ("body_attachment_note", "body", "attachment_note", True, False),
    ("body_issuing_signature", "body", "issuing_signature", False, False),
    ("body_issue_date", "body", "issue_date", False, False),
    ("body_stamp", "body", "stamp", True, False),
    ("body_note", "body", "note", True, False),
    ("body_attachments", "body", "attachments", True, False),
    # Footer section
    ("footer_closing_line", "footer", "closing_line", False, False),
    ("footer_carbon_copy", "footer", "carbon_copy", True, False),
    ("footer_issuing_office", "footer", "issuing_office", False, False),
    ("footer_distribution_date", "footer", "distribution_date", False, False),
    ("footer_page_number", "footer", "page_number", False, False),
]


# Chinese display names for document section fields (single source of truth).
# Format: "{section_attr}.{field_name}" → Chinese display name.
SECTION_FIELD_NAMES_CN: dict[str, str] = {
    "header.copy_number": "份号",
    "header.classification_duration": "密级和保密期限",
    "header.urgency_level": "紧急程度",
    "header.issuing_logo": "发文机关标志",
    "header.issuing_number": "发文字号",
    "header.signatory": "签发人",
    "header.ruling_line_pos": "红色分隔线",
    "body.title": "标题",
    "body.addressee": "主送机关",
    "body.main_text": "正文",
    "body.attachment_note": "附件说明",
    "body.issuing_signature": "发文机关署名",
    "body.issue_date": "成文日期",
    "body.stamp": "印章",
    "body.note": "附注",
    "body.attachments": "附件",
    "footer.closing_line": "黑色反线",
    "footer.carbon_copy": "抄送机关",
    "footer.issuing_office": "印发机关",
    "footer.distribution_date": "印发日期",
    "footer.page_number": "页码",
}


def iter_section_paragraphs(page_content: Any) -> Generator[
    tuple[Any, str, int], None, None
]:
    """Yield (paragraph, section_type, order_index) for every paragraph in a PageContent.

    Shared by save_doc and load_doc to eliminate duplicated section-type enumeration.
    """
    for section_type, section_attr, field_name, _optional, is_list in SECTION_FIELDS:
        section = getattr(page_content, section_attr)
        field_val = getattr(section, field_name, None)
        if field_val is None:
            continue
        if is_list:
            for idx, para in enumerate(field_val):
                yield para, section_type, idx
        else:
            yield field_val, section_type, 0
