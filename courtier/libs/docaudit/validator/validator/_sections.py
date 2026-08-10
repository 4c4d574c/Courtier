"""Document section-field → Chinese display-name mapping for the validator.

Single source of truth for the "{section_attr}.{field_name}" → 中文块名
mapping used by the format checker when reporting errors. Kept local to
the validator package so it no longer depends on ``courtier.db._utils``
(the db package has no use of this table).
"""

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
