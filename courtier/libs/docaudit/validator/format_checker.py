from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from docmodels import Block, DocumentFormatSpec

from courtier.db._utils import SECTION_FIELD_NAMES_CN as FIELD_NAME_CN

from ._fonts import FONT_FAMILY_EN_TO_CN, FONT_FAMILY_CN_ALIASES

logger = logging.getLogger(__name__)

# 规则文件所在目录
RULES_DIR = Path(__file__).parent / "rules"

# 公文类型 -> 规则文件名 映射
# 约定：规则文件命名为 "<公文类型>规则.json"
DOCUMENT_TYPE_MAP = {
    "通知": "通知规则.json",
    "函": "函规则.json",
    "请示": "请示规则.json",
}

SUPPORTED_TYPES = tuple(DOCUMENT_TYPE_MAP.keys())

# 大纲级别 -> 中文名称映射
OUTLINE_LEVEL_CN = {
    "heading1": "一级标题",
    "heading2": "二级标题",
    "heading3": "三级标题",
    "heading4": "四级标题",
    "heading5": "五级标题",
    "body_text": "正文",
    "others": "其他",
}


def normalize_font_family(font_family: str) -> str:
    """将英文字体名或中文别名统一为中文标准字体名。

    英文名（如 FangSong）→ 中文名（如 仿宋）
    中文别名（如 小标宋体）→ 中文标准名（如 小标宋）
    无法识别的名称原样返回。
    """
    if not font_family:
        return font_family

    # 先查英文名映射
    cn_name = FONT_FAMILY_EN_TO_CN.get(font_family)
    if cn_name:
        return cn_name

    # 再查中文别名映射
    standard = FONT_FAMILY_CN_ALIASES.get(font_family)
    if standard:
        return standard

    # 尝试不带空格、统一大小写后匹配
    key = font_family.strip()
    cn_name = FONT_FAMILY_EN_TO_CN.get(key)
    if cn_name:
        return cn_name

    return font_family


def compare_document_to_spec(doc: dict, spec: DocumentFormatSpec) -> dict[str, Any]:
    """
    将文档字典与 DocumentFormatSpec 进行比较。

    doc 为 parsed Document 的 dict 形式（如 parse_document 工具的输出）。

    返回可序列化为 JSON 的字典，包含：
        - total_pages: int
        - errors: list of dict，每个 dict 包含：
            - block_name: str   # 中文块名
            - block_no: int     # 块的序号
            - error_type: str   # 错误类型（中文）
            - details: str      # 详细描述（中文）
            - page_no: int      # 错误所属页码
    """
    # 构建 main_text 中各 outline_level 对应的 Block
    main_text_blocks_by_level: dict[str, Block] = {}
    main_text_obj = spec.page.body.main_text
    if isinstance(main_text_obj, dict):
        for level, block in main_text_obj.items():
            if isinstance(block, Block):
                main_text_blocks_by_level[level] = block
    elif hasattr(type(main_text_obj), "model_fields"):
        for level in type(main_text_obj).model_fields:
            block = getattr(main_text_obj, level, None)
            if isinstance(block, Block):
                main_text_blocks_by_level[level] = block

    # 构建路径到 Block 的映射，便于通过字段路径快速查找
    spec_block_paths: dict[tuple[str, ...], Block] = {}

    def record_paths(obj: Any, path: tuple[str, ...]) -> None:
        if isinstance(obj, Block):
            spec_block_paths[path] = obj
        elif isinstance(obj, dict):
            for k, v in obj.items():
                record_paths(v, path + (k,))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                record_paths(item, path + (str(i),))
        elif hasattr(obj, "__dict__"):
            for k, v in obj.__dict__.items():
                record_paths(v, path + (k,))
        elif hasattr(obj, "__slots__"):
            for slot in obj.__slots__:
                if hasattr(obj, slot):
                    record_paths(getattr(obj, slot), path + (slot,))

    record_paths(spec.page, ())

    total_pages = doc.get("total_page_num", 0)
    all_errors: list[dict[str, Any]] = []  # 汇总所有错误

    def should_exist_on_page(block_path: tuple[str, ...], page_no: int) -> bool:
        """根据块路径和页码判断该块是否应该存在于当前页。返回 True 表示应存在，False 表示不应存在（缺失时不报错）。"""
        first_page = 0
        last_page = total_pages - 1 if total_pages > 0 else 0
        # 版头相关块只在第1页
        if len(block_path) >= 2 and block_path[0] == "header":
            return page_no == first_page
        # 版记相关块只在最后一页
        if len(block_path) >= 2 and block_path[0] == "footer":
            if block_path == ("footer", "page_number"):
                return page_no > first_page
            return page_no == last_page
        # 标题和主送机关只在第1页
        if block_path == ("body", "title") or block_path == ("body", "addressee"):
            return page_no == first_page
        # 附件说明、署名、成文日期、印章、附注、附件只在最后一页
        if block_path in [
            ("body", "attachment_note"),
            ("body", "issuing_signature"),
            ("body", "issue_date"),
            ("body", "stamp"),
            ("body", "note"),
            ("body", "attachments"),
        ]:
            return page_no == last_page
        # 其他块（如正文段落）始终认为应存在
        return True

    def _check_element_font(
        actual_meta: dict,
        spec_elem: Any,
        block_name_cn: str,
        block_no: int,
        page_no: int,
        para_text: str,
    ) -> None:
        """Compare a single element's font properties against the spec and append errors."""
        spec_font = spec_elem.font
        actual_font = actual_meta.get("font", {})
        spec_family = normalize_font_family(spec_font.font_family)
        actual_family = normalize_font_family(actual_font.get("font_family", ""))
        mismatches: list[str] = []
        if spec_family and spec_family != actual_family:
            mismatches.append(f"字体应为「{spec_family}」，实际为「{actual_family}」")
        if (
            spec_font.font_size > 0
            and abs(float(spec_font.font_size) - float(actual_font.get("font_size", 0)))
            > 0.5
        ):
            mismatches.append(
                f"字号应为 {spec_font.font_size}，实际为 {actual_font.get('font_size', 0)}"
            )
        if spec_font.font_weight != actual_font.get("font_weight", False):
            expected_weight = "加粗" if spec_font.font_weight else "非加粗"
            actual_weight = (
                "加粗" if actual_font.get("font_weight", False) else "非加粗"
            )
            mismatches.append(f"字重应为{expected_weight}，实际为{actual_weight}")
        if spec_font.font_style != actual_font.get("font_style", False):
            expected_style = "斜体" if spec_font.font_style else "非斜体"
            actual_style = "斜体" if actual_font.get("font_style", False) else "非斜体"
            mismatches.append(f"字形应为{expected_style}，实际为{actual_style}")
        if mismatches:
            actual_text = actual_font.get("text", "")
            all_errors.append(
                {
                    "block_name": block_name_cn,
                    "block_no": block_no,
                    "error_type": "字体不匹配",
                    "details": f"文字行「{actual_text[:20]}」字体不符合规范："
                    + "；".join(mismatches),
                    "page_no": page_no,
                    "origin_text": actual_text or para_text,
                }
            )

    def compare_by_path(
        actual_val: dict | None,
        spec_path: tuple[str, ...],
        block_name_cn: str,
        page_no: int,
    ) -> None:
        """通过路径在 spec 中查找对应的 Block 并比较（使用中文块名），同时考虑页面存在性规则。"""
        if not should_exist_on_page(spec_path, page_no):
            return
        spec_block = spec_block_paths.get(spec_path)
        if spec_block:
            _compare_paragraph(
                all_errors, actual_val, spec_block, block_name_cn, page_no
            )

    for page in doc.get("pages", []):
        page_no = page.get("page_no", 0)
        page_content = page.get("page_content", {})

        # Header, Body, Footer field comparison (DRY via section-field lists)
        header = page_content.get("header", {})
        body = page_content.get("body", {})
        _HEADER_FIELDS = [
            "copy_number",
            "classification_duration",
            "urgency_level",
            "issuing_logo",
            "issuing_number",
            "signatory",
            "ruling_line_pos",
        ]
        _BODY_FIELDS = [
            "title",
            "addressee",
            "attachment_note",
            "issuing_signature",
            "issue_date",
            "stamp",
            "note",
            "attachments",
        ]
        for section_name, section_dict, fields in [
            ("header", header, _HEADER_FIELDS),
            ("body", body, _BODY_FIELDS),
        ]:
            for field in fields:
                compare_by_path(
                    section_dict.get(field),
                    (section_name, field),
                    FIELD_NAME_CN[f"{section_name}.{field}"],
                    page_no,
                )

        # Main text（正文段落列表）
        for idx, para in enumerate(body.get("main_text", [])):
            outline = para.get("outline_level", "others")
            outline_cn = OUTLINE_LEVEL_CN.get(outline, outline)
            block_name_cn = f"正文段落（{outline_cn}）"
            spec_block = main_text_blocks_by_level.get(outline)
            # 如果找不到对应的大纲级别，尝试使用 others
            if not spec_block:
                spec_block = main_text_blocks_by_level.get("others")
            # 如果还是没有对应的规范块，跳过验证（Word文档可能有其他类型的大纲）
            if spec_block:
                _compare_paragraph(
                    all_errors,
                    para,
                    spec_block,
                    block_name_cn,
                    page_no,
                    check_element_count=False,
                )
            # else: 忽略未找到大纲级别的情况，不报错

        # Footer
        footer = page_content.get("footer", {})
        _FOOTER_FIELDS = [
            "closing_line",
            "carbon_copy",
            "issuing_office",
            "distribution_date",
            "page_number",
        ]
        for field in _FOOTER_FIELDS:
            compare_by_path(
                footer.get(field),
                ("footer", field),
                FIELD_NAME_CN[f"footer.{field}"],
                page_no,
            )

    return {"total_pages": total_pages, "errors": all_errors}


def _compare_paragraph(
    all_errors: list,
    paragraph: dict | None,
    block: Block,
    block_name_cn: str,
    page_no: int,
    *,
    check_element_count: bool = True,
) -> None:
    """比较实际的 Paragraph dict 与 spec 中的 Block，如果发现差异则添加错误（带 page_no）。

    Args:
        all_errors: 累积错误列表，直接 append。
        paragraph: 段落 dict（parse_document 输出中的段落对象），可为 None
        check_element_count: 是否严格检查元素数量。对于固定结构块（如标题、发文字号）
            应设为 True；对于 main_text 中的动态段落（正文、各级标题），
            规则中的 elements 是格式模板，实际元素数量可能不同，应设为 False。
    """
    if paragraph is None:
        if block.required:
            all_errors.append(
                {
                    "block_name": block_name_cn,
                    "block_no": block.block_no,
                    "error_type": "缺失必要元素",
                    "details": f"必要块 {block_name_cn} 缺失。",
                    "page_no": page_no,
                    "origin_text": "",
                }
            )
        return

    # 提取段落原文
    para_text = "".join(
        e.get("font", {}).get("text", "")
        for e in paragraph.get("elements", [])
        if e.get("font", {}).get("text")
    )

    spec_elements = block.elements
    actual_elements = paragraph.get("elements", [])

    # 严格检查元素数量（仅用于固定结构块）
    if check_element_count and len(actual_elements) != len(spec_elements):
        if len(actual_elements) == 0:
            detail = f"该段落无文字行内容（期望 {len(spec_elements)} 行），可能存在解析异常或内容缺失。"
        else:
            detail = f"文字行数量不一致：期望 {len(spec_elements)} 行，实际 {len(actual_elements)} 行。"
        all_errors.append(
            {
                "block_name": block_name_cn,
                "block_no": block.block_no,
                "error_type": "元素数量不匹配",
                "details": detail,
                "page_no": page_no,
                "origin_text": para_text or "（无内容）",
            }
        )
        min_len = min(len(actual_elements), len(spec_elements))
    else:
        min_len = len(actual_elements)

    # 对于模板块（check_element_count=False），使用 spec_elements[0] 作为模板
    # 循环检查所有实际元素；对于固定块，按位置一对一比较
    for i in range(min_len):
        actual_meta = actual_elements[i]
        if check_element_count:
            spec_elem = spec_elements[i]
        else:
            # 模板模式：如果 spec 有多个元素模板且实际元素不超过模板数量，按位置匹配；
            # 否则全部使用第一个元素作为模板
            if i < len(spec_elements):
                spec_elem = spec_elements[i]
            else:
                spec_elem = spec_elements[0] if spec_elements else None

        if spec_elem is None:
            continue

        _check_element_font_module(
            all_errors,
            actual_meta,
            spec_elem,
            block_name_cn,
            block.block_no,
            page_no,
            para_text,
        )

    # 比较对齐方式（仅当 spec 中指定了非 left 的对齐方式时）
    if block.alignment and block.alignment != "left":
        if paragraph.get("alignment", "left") != block.alignment:
            all_errors.append(
                {
                    "block_name": block_name_cn,
                    "block_no": block.block_no,
                    "error_type": "对齐方式不匹配",
                    "details": f"对齐方式不一致：期望 {block.alignment}，实际 {paragraph.get('alignment', 'left')}",
                    "page_no": page_no,
                    "origin_text": para_text,
                }
            )


def _check_element_font_module(
    all_errors: list,
    actual_meta: dict,
    spec_elem: Any,
    block_name_cn: str,
    block_no: int,
    page_no: int,
    para_text: str,
) -> None:
    """Compare a single element's font properties against the spec and append errors (module-level helper)."""
    spec_font = spec_elem.font
    actual_font = actual_meta.get("font", {})
    spec_family = normalize_font_family(spec_font.font_family)
    actual_family = normalize_font_family(actual_font.get("font_family", ""))
    mismatches: list[str] = []
    if spec_family and spec_family != actual_family:
        mismatches.append(f"字体应为「{spec_family}」，实际为「{actual_family}」")
    if spec_font.font_size > 0 and abs(
        float(spec_font.font_size) - float(actual_font.get("font_size", 0))
    ) > 0.5:
        mismatches.append(
            f"字号应为 {spec_font.font_size}，实际为 {actual_font.get('font_size', 0)}"
        )
    if spec_font.font_weight != actual_font.get("font_weight", False):
        expected_weight = "加粗" if spec_font.font_weight else "非加粗"
        actual_weight = "加粗" if actual_font.get("font_weight", False) else "非加粗"
        mismatches.append(f"字重应为{expected_weight}，实际为{actual_weight}")
    if spec_font.font_style != actual_font.get("font_style", False):
        expected_style = "斜体" if spec_font.font_style else "非斜体"
        actual_style = "斜体" if actual_font.get("font_style", False) else "非斜体"
        mismatches.append(f"字形应为{expected_style}，实际为{actual_style}")
    if mismatches:
        actual_text = actual_font.get("text", "")
        all_errors.append(
            {
                "block_name": block_name_cn,
                "block_no": block_no,
                "error_type": "字体不匹配",
                "details": f"文字行「{actual_text[:20]}」字体不符合规范："
                + "；".join(mismatches),
                "page_no": page_no,
                "origin_text": actual_text or para_text,
            }
        )


def load_spec(doc_type: str) -> DocumentFormatSpec:
    """根据公文类型加载对应的格式规范。

    Args:
        doc_type: 公文类型，如"通知"、"函"

    Returns:
        DocumentFormatSpec 解析后的规范模型

    Raises:
        ValueError: 如果公文类型不在支持范围内
        FileNotFoundError: 如果规则文件不存在
    """
    if doc_type not in DOCUMENT_TYPE_MAP:
        raise ValueError(
            f"不支持的公文类型 '{doc_type}'，支持的类型：{', '.join(SUPPORTED_TYPES)}"
        )

    rule_file = RULES_DIR / DOCUMENT_TYPE_MAP[doc_type]
    if not rule_file.exists():
        raise FileNotFoundError(f"规则文件不存在：{rule_file}")

    with open(rule_file, "r", encoding="utf8") as f:
        spec_data = json.load(f)

    return DocumentFormatSpec.model_validate(spec_data["document_format_spec"])


def list_available_rules() -> list[str]:
    """列出所有可用的公文类型。"""
    return list(SUPPORTED_TYPES)


def validator(
    doc: dict, doc_type: str = "通知", spec: DocumentFormatSpec | None = None
) -> dict[str, Any]:
    """验证文档格式是否符合公文规范。

    Args:
        doc: 解析后的文档 dict（parse_document 工具的输出）
        doc_type: 公文类型，默认为"通知"
        spec: 可选的格式规范，若为 None 则从本地规则文件加载

    Returns:
        验证结果字典，包含 total_pages 和 errors
    """
    if spec is None:
        spec = load_spec(doc_type)
    return compare_document_to_spec(doc, spec)
