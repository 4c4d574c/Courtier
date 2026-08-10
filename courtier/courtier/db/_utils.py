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
#
# is_optional 现全部取 True：docmodels 重构后 Header/Body/Footer 的 17 个段落
# 槽位均为 Optional（None 表示该页无此要素），不再有必填槽位。保留该标志位
# 仅维持元组结构稳定，未来若重新引入必填槽位可直接使用。
_SECTION_ENTRY = tuple[str, str, str, bool, bool]

SECTION_FIELDS: list[_SECTION_ENTRY] = [
    # Header section
    ("header_copy_number", "header", "copy_number", True, False),
    ("header_classification_duration", "header", "classification_duration", True, False),
    ("header_urgency_level", "header", "urgency_level", True, False),
    ("header_issuing_logo", "header", "issuing_logo", True, False),
    ("header_issuing_number", "header", "issuing_number", True, False),
    ("header_signatory", "header", "signatory", True, False),
    ("header_ruling_line_pos", "header", "ruling_line_pos", True, False),
    # Body section
    ("body_title", "body", "title", True, False),
    ("body_addressee", "body", "addressee", True, False),
    ("body_main_text", "body", "main_text", True, True),
    ("body_attachment_note", "body", "attachment_note", True, False),
    ("body_issuing_signature", "body", "issuing_signature", True, False),
    ("body_issue_date", "body", "issue_date", True, False),
    ("body_stamp", "body", "stamp", True, False),
    ("body_note", "body", "note", True, False),
    ("body_attachments", "body", "attachments", True, False),
    # Footer section
    ("footer_closing_line", "footer", "closing_line", True, False),
    ("footer_carbon_copy", "footer", "carbon_copy", True, False),
    ("footer_issuing_office", "footer", "issuing_office", True, False),
    ("footer_distribution_date", "footer", "distribution_date", True, False),
    ("footer_page_number", "footer", "page_number", True, False),
]


def iter_section_paragraphs(page_content: Any) -> Generator[tuple[Any, str, int], None, None]:
    """Yield (paragraph, section_type, order_index) for every paragraph in a PageContent.

    Shared by save_doc and load_doc to eliminate duplicated section-type enumeration.
    值为 None 的槽位一律跳过：模型全 Optional 化后，空骨架（所有槽位为 None
    且 main_text 为空）不再产生任何占位段落行。
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
