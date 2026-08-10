from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from docmodels import Block, DocumentFormatSpec
from docmodels.constants import STANDARD_MARGINS

from ._fonts import FONT_FAMILY_CN_ALIASES, FONT_FAMILY_EN_TO_CN
from ._sections import SECTION_FIELD_NAMES_CN as FIELD_NAME_CN

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

# ---------------------------------------------------------------------------
# 间距/缩进/页边距检查约定
#
# 单位换算约定（规则 JSON 数值的读法，与 load_spec 加载的现有规则文件自洽）：
# - line_spacing：数值 < 10 视为"倍数"，乘以该块代表字号（spec elements 中
#   首个有效 font_size）换算为 pt；>= 10 视为 pt 绝对值。
#   例：通知规则 title line_spacing=1.5（1.5 × 22pt = 33pt），
#       body_text line_spacing=28.95（pt 绝对值）。
#   规则 JSON 可省略该键（GB/T 9704 不规定版头/版记块的行距），加载后
#   为 None，行距检查跳过并记入 unchecked。
# - first_indent：数值 < 10 视为"字符数"，乘以该块代表字号换算为 pt；
#   >= 10 视为 pt 绝对值。
#   例：body_text first_indent=2（2 字符 × 16pt = 32pt）。
# - space_before / space_after：始终为 pt 绝对值，无换算。
#
# 文档侧（docmodels.Paragraph）这些字段单位均为 pt，None 表示未提取：
# 未提取的字段跳过检查并记入返回值的 unchecked 列表，不产生错误。
# ---------------------------------------------------------------------------

SPACING_TOLERANCE_PT = 1.0  # 行距/段前间距/段后间距容差（pt）
FIRST_INDENT_TOLERANCE_PT = 2.0  # 首行缩进容差（pt）
MARGIN_TOLERANCE_MM = 1.0  # 页边距容差（mm）


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


def _spec_reference_font_size(block: Block) -> float | None:
    """规范块的代表字号（pt）：取 elements 中首个有效 font_size，无则返回 None。"""
    for el in block.elements:
        if el.font.font_size > 0:
            return float(el.font.font_size)
    return None


def _spacing_value_to_pt(value: float, reference_font_size: float | None) -> float | None:
    """按单位换算约定把规则中的 line_spacing / first_indent 数值换算为 pt。

    数值 < 10 为倍数/字符数，需乘以代表字号；>= 10 为 pt 绝对值。
    倍数/字符数缺少代表字号而无法换算时返回 None（该字段跳过检查）。
    """
    if value >= 10:
        return float(value)
    if reference_font_size is None or reference_font_size <= 0:
        return None
    return float(value) * reference_font_size


def _normalize_compare_text(text: str) -> str:
    """规范化用于比较的文本：去除所有空白字符。

    DOCX 按 run 分组，同一行文字的空白拆分位置不定，比较前整体去除。
    """
    return "".join(text.split())


def _iter_position_dicts(obj: Any):
    """递归找出 doc dict 中所有元素的 position 字典。"""
    if isinstance(obj, dict):
        pos = obj.get("position")
        if isinstance(pos, dict) and {"x0", "y0", "x1", "y1"} <= pos.keys():
            yield pos
        for v in obj.values():
            yield from _iter_position_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_position_dicts(item)


def _margins_are_reliable(doc: dict) -> bool:
    """判定页边距数据是否可靠（是否值得启用页边距检查）。

    优先读显式来源字段 ``doc["source"]``（docparse registry 在分派后回填）：
    - ``"docx"`` → 页边距直接取自 Word section 真实设置，可信 → 启用检查；
    - ``"pdf"`` / ``"scanned"`` → 页边距由文字 bbox 推算或按经验估算，
      噪声大，硬查会误报 → 跳过检查；
    - ``""``（旧缓存反序列化、或直接调用单个 parser 未回填）→ 回退
      "position 恒 0" 启发式：所有元素 position 恒为 0 → DOCX 来源 →
      启用；只要存在非 0 position → PDF/扫描来源 → 跳过。
    """
    source = doc.get("source") or ""
    if source == "docx":
        return True
    if source in ("pdf", "scanned"):
        return False
    for pos in _iter_position_dicts(doc):
        if any(float(pos.get(k, 0.0)) != 0.0 for k in ("x0", "y0", "x1", "y1")):
            return False
    return True


def compare_document_to_spec(doc: dict, spec: DocumentFormatSpec) -> dict[str, Any]:
    """
    将文档字典与 DocumentFormatSpec 进行比较。

    doc 为 parsed Document 的 dict 形式（如 parse_document 工具的输出）。

    返回可序列化为 JSON 的字典，包含：
        - total_pages: int
        - errors: list of dict，每个 dict 包含：
            - block_name: str   # 中文块名
            - block_no: int     # 规范侧块序号（spec 中的顺序号；文档侧模型已无此字段）
            - error_type: str   # 错误类型（中文）
            - details: str      # 详细描述（中文）
            - page_no: int      # 错误所属页码（1 基，面向用户）
            - origin_text: str  # 相关原文
        - unchecked: list of dict，被跳过的检查（文档侧字段未提取 None、
          或来源不可靠如 PDF/扫描推算页边距），不产生错误仅作区分；
          page_no 同为 1 基，文档级条目为 None
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
    unchecked: list[dict[str, Any]] = []  # 汇总被跳过的检查（文档侧未提取/来源不可靠）

    def should_exist_on_page(block_path: tuple[str, ...], page_no: int) -> bool:
        """根据块路径和页码判断该块是否应该存在于当前页。

        返回 True 表示应存在，False 表示不应存在（缺失时不报错）。
        page_no 为内部 0 基页码。
        """
        first_page = 0
        last_page = total_pages - 1 if total_pages > 0 else 0
        # 版头相关块只在第1页
        if len(block_path) >= 2 and block_path[0] == "header":
            return page_no == first_page
        # 版记相关块只在最后一页；页码只在第1页
        if len(block_path) >= 2 and block_path[0] == "footer":
            if block_path == ("footer", "page_number"):
                # DOCX 新语义下页眉页脚只随第一个页组携带，第 2 页起
                # footer.page_number 为 None 是"未携带"而非"缺失"，
                # 故页码只在首页检查（首页必须携带页码）。
                return page_no == first_page
            # 版记内容（抄送、印发机关等）来自正文流末尾，
            # DOCX 按显式分页符分页组时被分类到最后一个页组。
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

    def compare_by_path(
        actual_val: dict | None,
        spec_path: tuple[str, ...],
        block_name_cn: str,
        page_no: int,
    ) -> None:
        """通过路径在 spec 中查找对应的 Block 并比较（使用中文块名）。

        should_exist_on_page 只控制"缺失是否报错"：块在该页不应存在且实际
        缺失 → 跳过；但块一旦实际存在（如 PDF 第 2 页起的页码），无论是否
        "应该"存在于该页，都要照常比较字体/字号/间距。
        """
        if actual_val is None and not should_exist_on_page(spec_path, page_no):
            return
        spec_block = spec_block_paths.get(spec_path)
        if spec_block:
            _compare_paragraph(
                all_errors, unchecked, actual_val, spec_block, block_name_cn, page_no
            )

    # 页边距检查只在数据可靠（DOCX 来源，由 _margins_are_reliable 判定）时启用；
    # PDF/扫描来源的页边距为 bbox 推算/估算值，噪声大，硬查会误报。
    margins_reliable = _margins_are_reliable(doc)

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
                    unchecked,
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

        if margins_reliable:
            _check_page_margins(all_errors, unchecked, page, page_no)

    if not margins_reliable and doc.get("pages"):
        # 文档级跳过记录：页边距检查对整个文档未启用
        unchecked.append(
            {
                "block_name": "页面设置",
                "block_no": 0,
                "check": "margin",
                "fields": ["top_margin", "bottom_margin", "left_margin", "right_margin"],
                "page_no": None,
                "reason": "文档含实测版面坐标（PDF/扫描来源），页边距为推算值，跳过全部页检查",
            }
        )

    # 出口处统一把内部 0 基页码转为面向用户的 1 基页码
    for error in all_errors:
        error["page_no"] = int(error.get("page_no", 0)) + 1
    for item in unchecked:
        if item.get("page_no") is not None:
            item["page_no"] = int(item["page_no"]) + 1

    return {"total_pages": total_pages, "errors": all_errors, "unchecked": unchecked}


def _compare_paragraph(
    all_errors: list,
    unchecked: list,
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
        unchecked: 累积被跳过的检查项（如文档侧间距字段未提取），直接 append。
        paragraph: 段落 dict（parse_document 输出中的段落对象），可为 None
        check_element_count: 是否按固定结构块校验。固定块（如标题、发文字号）为 True，
            校验拼接文本（规范化空白后）是否为空——元素个数差异不再报错
            （DOCX 按 run 分组，一行多格式 = 多元素，个数差异是必然现象）；
            main_text 中的动态段落为 False，规则中的 elements 仅作格式模板。
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

    if check_element_count:
        # 固定槽位块：比较拼接后的文本内容（规范化空白后）而非元素个数。
        # 规范要求有文字内容而实际拼接文本为空，才是可靠的"内容缺失"信号。
        # 注意：不能与规范 elements 的文本比对内容——那只是格式样例
        # （如"份号：0001"），不是对实际文字的要求。
        spec_text = _normalize_compare_text(
            "".join(el.font.text for el in spec_elements if el.font.text)
        )
        if spec_text and not _normalize_compare_text(para_text):
            all_errors.append(
                {
                    "block_name": block_name_cn,
                    "block_no": block.block_no,
                    "error_type": "内容缺失",
                    "details": "该块无文字行内容，可能存在解析异常或内容缺失。",
                    "page_no": page_no,
                    "origin_text": para_text or "（无内容）",
                }
            )
    # 字体比对：
    # - 固定槽位块（check_element_count=True）：规则 elements 恒为 1 个格式
    #   样板，用首元素的字体规格比对所有实际元素——一行内混排格式（如
    #   "XXX局文件"前半黑体后半仿宋）若按位置一对一比对会漏检后续 run。
    # - 模板块（main_text，check_element_count=False）：spec elements 仅作
    #   格式模板，按位置匹配，超出模板数量的元素回退到首元素模板。
    for i, actual_meta in enumerate(actual_elements):
        if check_element_count or i >= len(spec_elements):
            spec_elem = spec_elements[0] if spec_elements else None
        else:
            spec_elem = spec_elements[i]

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
                    "details": (
                        f"对齐方式不一致：期望 {block.alignment}，"
                        f"实际 {paragraph.get('alignment', 'left')}"
                    ),
                    "page_no": page_no,
                    "origin_text": para_text,
                }
            )

    # 比较间距/缩进（文档侧字段为 None 表示未提取，跳过并记入 unchecked）
    _check_paragraph_spacing(
        all_errors, unchecked, paragraph, block, block_name_cn, page_no, para_text
    )


def _check_paragraph_spacing(
    all_errors: list,
    unchecked: list,
    paragraph: dict,
    block: Block,
    block_name_cn: str,
    page_no: int,
    para_text: str,
) -> None:
    """比较段落实测间距/缩进（pt）与规范值。

    文档侧字段为 None（未提取）时跳过该字段并记入 unchecked，不产生错误；
    容差：行距/段前/段后 ±1pt，首行缩进 ±2pt（见模块顶部单位换算约定）。
    """
    font_size = _spec_reference_font_size(block)
    checks = [
        ("space_before", "段前间距", float(block.space_before), SPACING_TOLERANCE_PT),
        ("space_after", "段后间距", float(block.space_after), SPACING_TOLERANCE_PT),
        (
            "line_spacing",
            "行距",
            (
                _spacing_value_to_pt(block.line_spacing, font_size)
                if block.line_spacing is not None
                else None
            ),
            SPACING_TOLERANCE_PT,
        ),
        (
            "first_indent",
            "首行缩进",
            _spacing_value_to_pt(block.first_indent, font_size),
            FIRST_INDENT_TOLERANCE_PT,
        ),
    ]
    mismatches: list[str] = []
    skipped_fields: list[str] = []
    for field, label, expected_pt, tolerance in checks:
        actual = paragraph.get(field)
        if actual is None or expected_pt is None:
            # actual 为 None：文档侧未提取该字段；
            # expected_pt 为 None：规范未规定该字段（规则 JSON 未设此键，
            # 如版头/版记块的行距），或规范值是倍数/字符数但缺代表字号，
            # 无法换算。
            skipped_fields.append(field)
            continue
        if abs(float(actual) - expected_pt) > tolerance:
            mismatches.append(
                f"{label}应为 {expected_pt:g}pt（容差±{tolerance:g}pt），"
                f"实际为 {float(actual):g}pt"
            )
    if skipped_fields:
        unchecked.append(
            {
                "block_name": block_name_cn,
                "block_no": block.block_no,
                "check": "spacing",
                "fields": skipped_fields,
                "page_no": page_no,
                "reason": "文档侧未提取该字段（None）、规范未规定或规范值无法换算，跳过该项检查",
            }
        )
    if mismatches:
        all_errors.append(
            {
                "block_name": block_name_cn,
                "block_no": block.block_no,
                "error_type": "间距/缩进不匹配",
                "details": "段落间距/缩进不符合规范：" + "；".join(mismatches),
                "page_no": page_no,
                "origin_text": para_text,
            }
        )


def _check_page_margins(all_errors: list, unchecked: list, page: dict, page_no: int) -> None:
    """比较单页页边距（mm）与 GB/T 9704-2012 标准值（上37/下35/左28/右26，容差±1mm）。

    仅由 _margins_are_reliable 判定可靠（DOCX 来源）时调用。
    某一边文档侧为 0.0 视为未提取（Margin 模型默认值；公文页边距不可能为 0），
    跳过该边并记入 unchecked。
    """
    page_content = page.get("page_content") or {}
    margin = page_content.get("margin") or {}
    mismatches: list[str] = []
    skipped_fields: list[str] = []
    for field, label, standard in (
        ("top_margin", "上边距", STANDARD_MARGINS["top"]),
        ("bottom_margin", "下边距", STANDARD_MARGINS["bottom"]),
        ("left_margin", "左边距", STANDARD_MARGINS["left"]),
        ("right_margin", "右边距", STANDARD_MARGINS["right"]),
    ):
        actual = float(margin.get(field) or 0.0)
        if actual <= 0:
            skipped_fields.append(field)
            continue
        if abs(actual - standard) > MARGIN_TOLERANCE_MM:
            mismatches.append(
                f"{label}应为 {standard:g}mm（容差±{MARGIN_TOLERANCE_MM:g}mm），"
                f"实际为 {actual:g}mm"
            )
    if skipped_fields:
        unchecked.append(
            {
                "block_name": "页面设置",
                "block_no": 0,
                "check": "margin",
                "fields": skipped_fields,
                "page_no": page_no,
                "reason": "文档侧未提取该边距（0.0），跳过该项检查",
            }
        )
    if mismatches:
        all_errors.append(
            {
                "block_name": "页面设置",
                "block_no": 0,
                "error_type": "页边距不匹配",
                "details": "页边距不符合 GB/T 9704-2012：" + "；".join(mismatches),
                "page_no": page_no,
                "origin_text": "",
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
    """Compare a single element's font properties against the spec and append errors.

    Module-level helper.
    """
    spec_font = spec_elem.font
    actual_font = actual_meta.get("font", {})
    spec_family = normalize_font_family(spec_font.font_family)
    actual_family = normalize_font_family(actual_font.get("font_family", ""))
    mismatches: list[str] = []
    if spec_family and spec_family != actual_family:
        mismatches.append(f"字体应为「{spec_family}」，实际为「{actual_family}」")
    if (
        spec_font.font_size > 0
        and abs(float(spec_font.font_size) - float(actual_font.get("font_size", 0))) > 0.5
    ):
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
                "details": f"文字行「{actual_text[:20]}」字体不符合规范：" + "；".join(mismatches),
                "page_no": page_no,
                "origin_text": actual_text or para_text,
            }
        )


def load_spec(doc_type: str) -> DocumentFormatSpec:
    """根据公文类型加载对应的格式规范。

    规则 JSON 中间距/缩进数值的单位换算约定（与现有规则文件自洽）：
    - line_spacing：数值 < 10 为"倍数"（×该块代表字号转 pt），>= 10 为 pt 绝对值。
      例：通知规则 title line_spacing=1.5（倍数）、body_text line_spacing=28.95（pt）。
    - first_indent：数值 < 10 为"字符数"（×该块代表字号转 pt），>= 10 为 pt 绝对值。
      例：body_text first_indent=2（字符，即 2×16pt=32pt）。
    - space_before / space_after：恒为 pt 绝对值，无换算。

    Args:
        doc_type: 公文类型，如"通知"、"函"

    Returns:
        DocumentFormatSpec 解析后的规范模型

    Raises:
        ValueError: 如果公文类型不在支持范围内
        FileNotFoundError: 如果规则文件不存在
    """
    if doc_type not in DOCUMENT_TYPE_MAP:
        raise ValueError(f"不支持的公文类型 '{doc_type}'，支持的类型：{', '.join(SUPPORTED_TYPES)}")

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
