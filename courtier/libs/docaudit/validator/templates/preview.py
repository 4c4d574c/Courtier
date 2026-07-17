"""模板预览。

将新格式模板规范（v1.0 hierarchical）转换为可预览的 Document 对象，
以及印章默认值处理。
"""
from __future__ import annotations

import base64
import os
from typing import Any

from docmodels.document import Body, Document, Footer, Header, Margin, Page, PageContent

from validator.templates.skeleton import _block_to_paragraph

_SAMPLE_TEXTS: dict[str, str] = {
    "copy_number": "000001",
    "classification_duration": "机密★1年",
    "urgency_level": "特急",
    "issuing_logo": "XX市人民政府文件",
    "issuing_number": "X政发〔2026〕1号",
    "signatory": "签发人：张三",
    "ruling_line_pos": "",
    "title": "关于XXXX的通知",
    "addressee": "各区、县人民政府：",
    "heading1": "一、工作背景",
    "heading2": "（一）具体事项",
    "heading3": "1. 细节说明",
    "heading4": "（1）子项说明",
    "heading5": "① 更细项说明",
    "body_text": "这是正文示例内容。为贯彻落实XXXX精神，根据XXXX要求，现就有关事项通知如下：",
    "others": "其他内容示例。",
    "attachment_note": "附件：1.XXXXXX",
    "issuing_signature": "XX市人民政府",
    "issue_date": "2026年5月11日",
    "stamp": "[印章]",
    "note": "（此件公开发布）",
    "attachments": "附件内容示例。",
    "closing_line": "━━━━━━━━━━━━━━━━━━━━━",
    "carbon_copy": "抄送：市委各部门，市人大常委会办公厅。",
    "issuing_office": "XX市人民政府办公厅",
    "distribution_date": "2026年5月11日印发",
    "page_number": "— 1 —",
}


def _build_block_from_element(el: dict[str, Any]) -> dict[str, Any]:
    """从新格式 element 构建预览用的 block 字典（兼容 _block_to_paragraph）。

    新格式 element:
        {"key": "title", "name": "标题", "category": "body",
         "format": {"space_before": 12, ...}, "content_elements": [...]}
    """
    fmt = el.get("format", {})
    content_elements = el.get("content_elements", [])
    ce = content_elements[0] if content_elements else {}
    font = ce.get("font", {}) if isinstance(ce, dict) else {}

    return {
        "required": el.get("required", False),
        "space_before": fmt.get("space_before", 0),
        "space_after": fmt.get("space_after", 0),
        "line_spacing": fmt.get("line_spacing", 1.0),
        "first_indent": fmt.get("first_indent", 0),
        "block_no": el.get("sort_order", 0),
        "outline_level": fmt.get("outline_level", "others"),
        "alignment": fmt.get("alignment", "left"),
        "elements": [
            {
                "position": {
                    "x0": font.get("position", {}).get("x0_mm", 0),
                    "y0": font.get("position", {}).get("y0_mm", 0),
                    "x1": font.get("position", {}).get("x1_mm", 0),
                    "y1": font.get("position", {}).get("y1_mm", 0),
                },
                "font": {
                    "font_family": font.get("font_family", ""),
                    "font_size": font.get("font_size", 0),
                    "font_weight": font.get("font_weight", False),
                    "font_style": font.get("font_style", False),
                    "text": font.get("text", ""),
                    "line_no": font.get("line_no", 1),
                },
            }
        ],
    }


def spec_from_template_content(content: dict) -> dict:
    """提取并返回新格式模板内容。

    仅接受新格式（version + page + elements），其他格式视为无效。
    """
    if not isinstance(content, dict):
        raise TypeError("template content must be a dict")
    # New format: has 'version' and 'elements'
    if "version" in content and "elements" in content:
        return content
    raise ValueError(
        "Unsupported template format: expected new format with 'version' and 'elements'"
    )


def spec_to_sample_document(template_json: dict) -> Document:
    """从新格式模板直接构建 Document 对象（填充示例文本）。

    Args:
        template_json: 新格式模板字典，包含 page（纸张/边距）和 elements 数组。
    """
    page_info = template_json.get("page", {})
    margins = page_info.get("margins", {})

    elements = template_json.get("elements", [])
    header_blocks: dict[str, dict[str, Any]] = {}
    body_blocks: dict[str, dict[str, Any]] = {}
    footer_blocks: dict[str, dict[str, Any]] = {}
    main_text_dict: dict[str, dict[str, Any]] = {}

    for el in elements:
        key = el.get("key")
        category = el.get("category")
        if not key or not category:
            continue

        block = _build_block_from_element(el)

        if category == "header":
            header_blocks[key] = block
        elif category == "footer":
            footer_blocks[key] = block
        elif category == "body":
            if key in (
                "heading1", "heading2", "heading3", "heading4", "heading5",
                "body_text", "others",
            ):
                main_text_dict[key] = block
            else:
                body_blocks[key] = block

    main_text_paras = [
        _block_to_paragraph(block, _SAMPLE_TEXTS.get(key, "示例"))
        for key in ("heading1", "heading2", "heading3", "heading4", "heading5",
                     "body_text", "others")
        if (block := main_text_dict.get(key))
    ]

    pc = PageContent(
        header=Header(
            copy_number=_block_to_paragraph(
                header_blocks.get("copy_number"), _SAMPLE_TEXTS["copy_number"]
            ),
            classification_duration=_block_to_paragraph(
                header_blocks.get("classification_duration"),
                _SAMPLE_TEXTS["classification_duration"],
            ),
            urgency_level=_block_to_paragraph(
                header_blocks.get("urgency_level"), _SAMPLE_TEXTS["urgency_level"]
            ),
            issuing_logo=_block_to_paragraph(
                header_blocks.get("issuing_logo"),
                _SAMPLE_TEXTS["issuing_logo"],
                default_font_size=28.0,
            ),
            issuing_number=_block_to_paragraph(
                header_blocks.get("issuing_number"), _SAMPLE_TEXTS["issuing_number"]
            ),
            signatory=_block_to_paragraph(
                header_blocks.get("signatory"), _SAMPLE_TEXTS["signatory"]
            ),
            ruling_line_pos=_block_to_paragraph(
                header_blocks.get("ruling_line_pos"), ""
            ),
        ),
        body=Body(
            title=_block_to_paragraph(
                body_blocks.get("title"), _SAMPLE_TEXTS["title"], default_font_size=22.0
            ),
            addressee=_block_to_paragraph(
                body_blocks.get("addressee"), _SAMPLE_TEXTS["addressee"]
            ),
            main_text=main_text_paras,
            attachment_note=_block_to_paragraph(
                body_blocks.get("attachment_note"), _SAMPLE_TEXTS["attachment_note"]
            ),
            issuing_signature=_block_to_paragraph(
                body_blocks.get("issuing_signature"), _SAMPLE_TEXTS["issuing_signature"]
            ),
            issue_date=_block_to_paragraph(
                body_blocks.get("issue_date"), _SAMPLE_TEXTS["issue_date"]
            ),
            stamp=_block_to_paragraph(body_blocks.get("stamp"), _SAMPLE_TEXTS["stamp"]),
            note=_block_to_paragraph(body_blocks.get("note"), _SAMPLE_TEXTS["note"]),
            attachments=_block_to_paragraph(
                body_blocks.get("attachments"), _SAMPLE_TEXTS["attachments"]
            ),
        ),
        footer=Footer(
            closing_line=_block_to_paragraph(
                footer_blocks.get("closing_line"), _SAMPLE_TEXTS["closing_line"]
            ),
            carbon_copy=_block_to_paragraph(
                footer_blocks.get("carbon_copy"), _SAMPLE_TEXTS["carbon_copy"]
            ),
            issuing_office=_block_to_paragraph(
                footer_blocks.get("issuing_office"), _SAMPLE_TEXTS["issuing_office"]
            ),
            distribution_date=_block_to_paragraph(
                footer_blocks.get("distribution_date"),
                _SAMPLE_TEXTS["distribution_date"],
            ),
            page_number=_block_to_paragraph(
                footer_blocks.get("page_number"), _SAMPLE_TEXTS["page_number"]
            ),
        ),
        margin=Margin(
            top_margin=margins.get("top", 37),
            bottom_margin=margins.get("bottom", 35),
            left_margin=margins.get("left", 28),
            right_margin=margins.get("right", 26),
        ),
    )

    return Document(
        user_id="preview",
        doc_id="preview",
        total_page_num=1,
        save_path="",
        pages=[Page(raw=b"", page_content=pc, save_path="", page_no=1)],
    )


# ===================== 默认印章图片处理 =====================

_DEFAULT_SEAL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "seals", "default_seal.png"
)

_default_seal_base64: str | None = None


def get_default_seal_base64() -> str:
    """读取默认印章图片并返回 data URI 格式的 base64 字符串。"""
    global _default_seal_base64
    if _default_seal_base64 is not None:
        return _default_seal_base64

    if not os.path.exists(_DEFAULT_SEAL_PATH):
        raise FileNotFoundError(f"默认印章图片不存在: {_DEFAULT_SEAL_PATH}")

    with open(_DEFAULT_SEAL_PATH, "rb") as f:
        img_data = f.read()
    _default_seal_base64 = "data:image/png;base64," + base64.b64encode(img_data).decode(
        "utf-8"
    )
    return _default_seal_base64


def replace_default_seal(content: dict) -> dict:
    """将模板 content 中印章元素的 '__default_seal__' 替换为默认印章图片的 base64。"""
    if not content or not isinstance(content, dict):
        return content

    elements = content.get("elements")
    if not elements or not isinstance(elements, list):
        return content

    try:
        default_seal = get_default_seal_base64()
    except FileNotFoundError:
        default_seal = ""

    for el in elements:
        if (
            isinstance(el, dict)
            and el.get("type") == "印章"
            and isinstance(el.get("style"), dict)
            and el["style"].get("sealImage") == "__default_seal__"
        ):
            el["style"]["sealImage"] = default_seal

    return content
