"""Structure collection utilities for scanned documents.

Provides paragraph collection from PageContent for spacing/indent merging.
"""

from __future__ import annotations

from docmodels import PageContent, Paragraph


def collect_all_paragraphs(
    page_content: PageContent,
) -> list[Paragraph]:
    """Collect all Paragraph objects from a PageContent."""
    paragraphs: list[Paragraph] = []

    # Header paragraphs
    header = page_content.header
    for field_name in (
        "issuing_logo",
        "issuing_number",
        "signatory",
        "ruling_line_pos",
        "copy_number",
        "classification_duration",
        "urgency_level",
    ):
        para = getattr(header, field_name, None)
        if para:
            paragraphs.append(para)

    # Body paragraphs
    body = page_content.body
    for field_name in (
        "title",
        "addressee",
        "issuing_signature",
        "issue_date",
    ):
        para = getattr(body, field_name, None)
        if para:
            paragraphs.append(para)
    for field_name in (
        "attachment_note",
        "stamp",
        "note",
        "attachments",
    ):
        para = getattr(body, field_name, None)
        if para:
            paragraphs.append(para)
    paragraphs.extend(body.main_text)

    # Footer paragraphs
    footer = page_content.footer
    for field_name in (
        "closing_line",
        "issuing_office",
        "distribution_date",
        "page_number",
        "carbon_copy",
    ):
        para = getattr(footer, field_name, None)
        if para:
            paragraphs.append(para)

    return paragraphs
