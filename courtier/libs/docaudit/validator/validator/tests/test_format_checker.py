"""format_checker 比较逻辑测试。

覆盖：间距/缩进检查（含单位换算约定与 None 跳过三态）、页边距检查
（Document.source 判定：docx 启用 / pdf、scanned、mixed 跳过 / 空串回退
position 启发式）、固定槽位块拼接文本比较（元素计数误报修复）、固定槽位块混排
run 字体全覆盖、page_no 1 基输出、DOCX 多页页码"首页携带"语义、
PDF 多页页码"存在即比对"语义。
"""

from __future__ import annotations

from typing import Any

import pytest
from docmodels import (
    Body,
    DocumentFormatSpec,
    Font,
    LineElement,
    PageContent,
    Paragraph,
    Position,
)
from docparse.parsers.scanned.spacing import merge_spacing_into_page_content
from docparse.parsers.spacing import compute_paragraph_spacing

from validator.format_checker import (
    _margins_are_reliable,
    _spacing_value_to_pt,
    compare_document_to_spec,
)

# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

_STANDARD_MARGIN = {
    "top_margin": 37.0,
    "bottom_margin": 35.0,
    "left_margin": 28.0,
    "right_margin": 26.0,
}


def _spec_element(text: str, font_size: float = 16.0) -> dict:
    return {
        "position": {"x0": 0, "y0": 0, "x1": 0, "y1": 0},
        "font": {
            "font_family": "仿宋",
            "font_size": font_size,
            "font_weight": False,
            "font_style": False,
            "text": text,
            "line_no": 0,
        },
    }


def _spec_block(
    text: str,
    *,
    required: bool = True,
    space_before: float = 0,
    space_after: float = 0,
    line_spacing: float = 28.95,
    first_indent: float = 0,
    font_size: float = 16.0,
    block_no: int = 1,
    outline_level: str = "others",
) -> dict:
    return {
        "required": required,
        "space_before": space_before,
        "space_after": space_after,
        "line_spacing": line_spacing,
        "first_indent": first_indent,
        "block_no": block_no,
        "outline_level": outline_level,
        "elements": [_spec_element(text, font_size)],
    }


def _make_spec() -> DocumentFormatSpec:
    """最小合法规范。

    - title：倍数行距 1.5（×22pt = 33pt）、段前 12 / 段后 6；
    - body_text：pt 绝对行距 28.95、首行缩进 2 字符（×16pt = 32pt）。
    """
    spec_dict = {
        "page": {
            "page_size": "A4",
            "page_dimensions": {"width": 210, "height": 297},
            "margin": {
                "top_margin": 37,
                "bottom_margin": 35,
                "left_margin": 28,
                "right_margin": 26,
            },
            "header": {
                "issuing_logo": _spec_block("XX市人民政府文件", font_size=22, block_no=1),
                "issuing_number": _spec_block("X政发〔2026〕1号", block_no=2),
                "ruling_line_pos": _spec_block("", block_no=3),
            },
            "body": {
                "title": _spec_block(
                    "关于XXXX的通知",
                    space_before=12,
                    space_after=6,
                    line_spacing=1.5,
                    font_size=22,
                    block_no=4,
                ),
                "addressee": _spec_block("各区、县人民政府：", block_no=5),
                "main_text": {
                    "body_text": _spec_block(
                        "正文示例。",
                        line_spacing=28.95,
                        first_indent=2,
                        block_no=6,
                        outline_level="body_text",
                    ),
                },
                "issuing_signature": _spec_block("XX市人民政府", block_no=7),
                "issue_date": _spec_block("2026年5月11日", block_no=8),
                "stamp": _spec_block("[印章]", block_no=9),
            },
            "footer": {
                "closing_line": _spec_block("━━━━", block_no=10),
                "issuing_office": _spec_block("XX市人民政府办公厅", block_no=11),
                "distribution_date": _spec_block("2026年5月11日印发", block_no=12),
                "page_number": _spec_block("— 1 —", block_no=13),
            },
        }
    }
    return DocumentFormatSpec.model_validate(spec_dict)


def _element_dict(text: str, font_size: float = 16.0, *, x0: float = 0.0) -> dict[str, Any]:
    return {
        "position": {"x0": x0, "y0": 0.0, "x1": x0, "y1": 0.0},
        "font": {
            "font_family": "仿宋",
            "font_size": font_size,
            "font_weight": False,
            "font_style": False,
            "text": text,
            "line_no": 0,
        },
    }


def _para_dict(
    text: str,
    *,
    outline: str = "others",
    font_size: float = 16.0,
    space_before: float | None = None,
    space_after: float | None = None,
    line_spacing: float | None = None,
    first_indent: float | None = None,
    elements: list | None = None,
) -> dict[str, Any]:
    if elements is None:
        elements = [_element_dict(text, font_size)] if text else []
    return {
        "space_before": space_before,
        "space_after": space_after,
        "line_spacing": line_spacing,
        "first_indent": first_indent,
        "left_indent": None,
        "right_indent": None,
        "elements": elements,
        "outline_level": outline,
        "alignment": "left",
    }


def _make_page(
    page_no: int,
    *,
    first_page: bool = True,
    last_page: bool = True,
    with_page_number: bool = True,
    margin: dict | None = None,
) -> dict[str, Any]:
    """构造一页合规 dict：必需槽位齐全、字体匹配规范、间距字段 None（跳过）。"""
    header: dict[str, Any] = {}
    body: dict[str, Any] = {}
    footer: dict[str, Any] = {}
    if first_page:
        header = {
            "issuing_logo": _para_dict("XX市人民政府文件", font_size=22),
            "issuing_number": _para_dict("X政发〔2026〕1号"),
            "ruling_line_pos": _para_dict(""),
        }
        body["title"] = _para_dict("关于XXXX的通知", font_size=22)
        body["addressee"] = _para_dict("各区、县人民政府：")
    body["main_text"] = [
        _para_dict(
            "正文内容。",
            outline="body_text",
            space_before=0.0,
            space_after=0.0,
            line_spacing=28.95,
            first_indent=32.0,
        )
    ]
    if last_page:
        body["issuing_signature"] = _para_dict("XX市人民政府")
        body["issue_date"] = _para_dict("2026年5月11日")
        body["stamp"] = _para_dict("[印章]")
        footer["closing_line"] = _para_dict("━━━━")
        footer["issuing_office"] = _para_dict("XX市人民政府办公厅")
        footer["distribution_date"] = _para_dict("2026年5月11日印发")
    if first_page and with_page_number:
        footer["page_number"] = _para_dict("— 1 —")
    return {
        "page_no": page_no,
        "page_content": {
            "header": header,
            "body": body,
            "footer": footer,
            "margin": margin or dict(_STANDARD_MARGIN),
        },
    }


def _make_doc(pages: list, **extra) -> dict[str, Any]:
    doc = {"total_page_num": len(pages), "pages": pages}
    doc.update(extra)
    return doc


def _errors(result: dict, error_type: str | None = None, block_name: str | None = None) -> list:
    errs = result["errors"]
    if error_type is not None:
        errs = [e for e in errs if e["error_type"] == error_type]
    if block_name is not None:
        errs = [e for e in errs if block_name in e["block_name"]]
    return errs


# ---------------------------------------------------------------------------
# 单位换算约定
# ---------------------------------------------------------------------------


class TestSpacingUnitConversion:
    """line_spacing/first_indent 的 <10 倍数(字符)×字号、>=10 pt 绝对值约定。"""

    def test_line_spacing_multiple(self) -> None:
        assert _spacing_value_to_pt(1.5, 22.0) == 33.0

    def test_line_spacing_absolute_pt(self) -> None:
        assert _spacing_value_to_pt(28.95, 16.0) == 28.95

    def test_first_indent_chars(self) -> None:
        assert _spacing_value_to_pt(2.0, 16.0) == 32.0

    def test_first_indent_absolute_pt(self) -> None:
        assert _spacing_value_to_pt(32.0, 16.0) == 32.0

    def test_multiple_without_font_size_unconvertible(self) -> None:
        assert _spacing_value_to_pt(1.5, None) is None
        assert _spacing_value_to_pt(1.5, 0.0) is None


# ---------------------------------------------------------------------------
# 间距/缩进检查
# ---------------------------------------------------------------------------


class TestParagraphSpacing:
    def test_spacing_ok_within_tolerance(self) -> None:
        """合格：行距 ±1pt、首行缩进 ±2pt 容差内，不产生错误。"""
        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = [
            _para_dict(
                "正文内容。",
                outline="body_text",
                space_before=0.5,
                space_after=0.0,
                line_spacing=28.0,  # 28.95 - 0.95，容差内
                first_indent=33.5,  # 32 + 1.5，容差内
            )
        ]
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "间距/缩进不匹配") == []

    def test_spacing_exceeds_tolerance(self) -> None:
        """超差：行距/首行缩进超出容差，报错并给出换算后的规范值。"""
        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = [
            _para_dict(
                "正文内容。",
                outline="body_text",
                space_before=5.0,
                space_after=0.0,
                line_spacing=25.0,
                first_indent=16.0,
            )
        ]
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "间距/缩进不匹配", "正文")
        assert len(errs) == 1
        details = errs[0]["details"]
        assert "行距应为 28.95pt" in details
        assert "首行缩进应为 32pt" in details  # 2 字符 × 16pt
        assert "段前间距" in details

    def test_spacing_none_skipped(self) -> None:
        """None 跳过：文档侧字段为 None（未提取）时不报错，记入 unchecked。"""
        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = [
            _para_dict("正文内容。", outline="body_text")  # 间距字段全 None
        ]
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "间距/缩进不匹配") == []
        spacing_unchecked = [u for u in result["unchecked"] if u.get("check") == "spacing"]
        assert spacing_unchecked, "应记录被跳过的间距检查"
        fields = spacing_unchecked[0]["fields"]
        assert set(fields) == {
            "space_before",
            "space_after",
            "line_spacing",
            "first_indent",
        }

    def test_line_spacing_multiple_rule(self) -> None:
        """倍数写法：title line_spacing=1.5（×22pt=33pt），实测 33pt 合格。"""
        page = _make_page(0)
        page["page_content"]["body"]["title"] = _para_dict(
            "关于XXXX的通知",
            font_size=22,
            space_before=12.0,
            space_after=6.0,
            line_spacing=33.0,
            first_indent=0.0,
        )
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "间距/缩进不匹配", "标题") == []

    def test_line_spacing_multiple_rule_mismatch(self) -> None:
        """倍数写法超差：实测 22pt（1 倍），期望 33pt（1.5 倍）。"""
        page = _make_page(0)
        page["page_content"]["body"]["title"] = _para_dict(
            "关于XXXX的通知",
            font_size=22,
            space_before=12.0,
            space_after=6.0,
            line_spacing=22.0,
            first_indent=0.0,
        )
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "间距/缩进不匹配", "标题")
        assert len(errs) == 1
        assert "行距应为 33pt" in errs[0]["details"]

    def test_space_before_after_checked_as_pt(self) -> None:
        """段前/段后恒为 pt：title 规范 12/6，实测偏离即报错。"""
        page = _make_page(0)
        page["page_content"]["body"]["title"] = _para_dict(
            "关于XXXX的通知",
            font_size=22,
            space_before=0.0,
            space_after=0.0,
            line_spacing=33.0,
            first_indent=0.0,
        )
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "间距/缩进不匹配", "标题")
        assert len(errs) == 1
        assert "段前间距应为 12pt" in errs[0]["details"]
        assert "段后间距应为 6pt" in errs[0]["details"]

    def test_scanned_single_line_page_line_spacing_unchecked(self) -> None:
        """扫描单行页：行距无法测量（None）→ 不产行距误报，记入 unchecked。

        走 docparse 扫描侧真实函数：compute_paragraph_spacing（单行页
        line_spacing=None，而非旧的硬编码 0.0）→ merge_spacing_into_page_content
        → model_dump(exclude_none=True)（与 parse_layout 插件输出一致）。
        """
        para = Paragraph(
            elements=[
                LineElement(
                    position=Position(x0=20.0, y0=100.0, x1=500.0, y1=120.0),
                    font=Font(text="单行正文。", line_no=0),
                )
            ],
            outline_level="body_text",
        )
        page_content = PageContent(body=Body(main_text=[para]))
        spacing_map = compute_paragraph_spacing([[20, 100, 500, 120]], 943, 841.89)
        assert spacing_map[0]["line_spacing"] is None  # 单行页行距未测得
        merge_spacing_into_page_content(page_content, spacing_map, {})
        assert para.line_spacing is None

        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = page_content.model_dump(exclude_none=True)[
            "body"
        ]["main_text"]
        result = compare_document_to_spec(_make_doc([page], source="scanned"), _make_spec())
        # 修复前：line_spacing=0.0 与规则 28.95pt 比对 → 行距误报
        assert _errors(result, "间距/缩进不匹配") == []
        spacing_unchecked = [u for u in result["unchecked"] if u.get("check") == "spacing"]
        assert any("line_spacing" in u["fields"] for u in spacing_unchecked)


# ---------------------------------------------------------------------------
# 页边距检查
# ---------------------------------------------------------------------------


class TestMargins:
    def test_margins_are_reliable_docx(self) -> None:
        """source="docx" → 页边距可信（即使存在非 0 position，显式来源优先）。"""
        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = [
            _para_dict(
                "正文内容。",
                outline="body_text",
                elements=[_element_dict("正文内容。", x0=100.0)],
            )
        ]
        assert _margins_are_reliable(_make_doc([page], source="docx")) is True

    def test_margins_are_reliable_pdf(self) -> None:
        """source="pdf" → 页边距为推算值，不可信（position 恒 0 也不启用）。"""
        assert _margins_are_reliable(_make_doc([_make_page(0)], source="pdf")) is False

    def test_margins_are_reliable_scanned(self) -> None:
        """source="scanned" → 页边距为估算值，不可信。"""
        assert _margins_are_reliable(_make_doc([_make_page(0)], source="scanned")) is False

    def test_margins_are_reliable_mixed(self) -> None:
        """source="mixed" → 含 OCR 页，页边距仍带推算成分，不可信。"""
        assert _margins_are_reliable(_make_doc([_make_page(0)], source="mixed")) is False

    def test_margins_are_reliable_fallback_heuristic(self) -> None:
        """source=""（旧缓存）→ 回退 position 启发式：恒 0 可信、有非 0 不可信。"""
        assert _margins_are_reliable(_make_doc([_make_page(0)])) is True
        page = _make_page(0)
        page["page_content"]["body"]["main_text"] = [
            _para_dict(
                "正文内容。",
                outline="body_text",
                elements=[_element_dict("正文内容。", x0=100.0)],
            )
        ]
        assert _margins_are_reliable(_make_doc([page])) is False

    def test_source_pdf_skips_margin_check(self) -> None:
        """compare 层：source="pdf" 时页边距再离谱也不检查，记入文档级 unchecked。"""
        page = _make_page(0, margin=dict(_STANDARD_MARGIN, top_margin=10.0))
        result = compare_document_to_spec(_make_doc([page], source="pdf"), _make_spec())
        assert _errors(result, "页边距不匹配") == []
        margin_unchecked = [u for u in result["unchecked"] if u.get("check") == "margin"]
        assert len(margin_unchecked) == 1
        assert margin_unchecked[0]["page_no"] is None  # 文档级条目

    def test_docx_margin_checked(self) -> None:
        """DOCX 来源：页边距偏离标准值（±1mm）报错。"""
        margin = dict(_STANDARD_MARGIN, top_margin=30.0)
        result = compare_document_to_spec(_make_doc([_make_page(0, margin=margin)]), _make_spec())
        errs = _errors(result, "页边距不匹配")
        assert len(errs) == 1
        assert "上边距应为 37mm" in errs[0]["details"]
        assert errs[0]["page_no"] == 1  # 1 基输出

    def test_docx_margin_standard_ok(self) -> None:
        result = compare_document_to_spec(_make_doc([_make_page(0)]), _make_spec())
        assert _errors(result, "页边距不匹配") == []

    def test_pdf_margin_skipped(self) -> None:
        """PDF 来源：页边距再离谱也不检查，记入文档级 unchecked。"""
        page = _make_page(0, margin=dict(_STANDARD_MARGIN, top_margin=10.0))
        page["page_content"]["body"]["main_text"] = [
            _para_dict(
                "正文内容。",
                outline="body_text",
                elements=[_element_dict("正文内容。", x0=100.0)],
            )
        ]
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "页边距不匹配") == []
        margin_unchecked = [u for u in result["unchecked"] if u.get("check") == "margin"]
        assert len(margin_unchecked) == 1
        assert margin_unchecked[0]["page_no"] is None  # 文档级条目


# ---------------------------------------------------------------------------
# 固定槽位块：拼接文本比较（元素计数误报修复）
# ---------------------------------------------------------------------------


class TestFixedBlockTextComparison:
    def test_multi_run_same_text_no_error(self) -> None:
        """同一行文字被 DOCX 拆成多个 run（元素个数 != 规范个数）不报错。"""
        runs = [
            _element_dict("关于", 22),
            _element_dict("XXXX", 22),
            _element_dict("的通知", 22),
        ]
        page = _make_page(0)
        page["page_content"]["body"]["title"] = _para_dict("", font_size=22, elements=runs)
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "元素数量不匹配") == []
        assert _errors(result, "内容缺失", "标题") == []

    def test_empty_fixed_block_reports_content_missing(self) -> None:
        """固定块存在但拼接文本为空 → 内容缺失。"""
        page = _make_page(0)
        page["page_content"]["body"]["title"] = _para_dict("")
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "内容缺失", "标题")
        assert len(errs) == 1

    def test_missing_required_block_still_reported(self) -> None:
        """固定块整体缺失（None）→ 缺失必要元素（既有行为保留）。"""
        page = _make_page(0)
        page["page_content"]["body"]["title"] = None
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "缺失必要元素", "标题")
        assert len(errs) == 1


# ---------------------------------------------------------------------------
# page_no 1 基输出
# ---------------------------------------------------------------------------


class TestPageNoOneBased:
    def test_error_on_first_page_is_page_1(self) -> None:
        page = _make_page(0)
        page["page_content"]["body"]["title"] = None
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "缺失必要元素", "标题")
        assert len(errs) == 1
        assert errs[0]["page_no"] == 1

    def test_error_on_second_page_is_page_2(self) -> None:
        pages = [
            _make_page(0, first_page=True, last_page=False),
            _make_page(1, first_page=False, last_page=True),
        ]
        pages[1]["page_content"]["body"]["issue_date"] = None
        result = compare_document_to_spec(_make_doc(pages), _make_spec())
        errs = _errors(result, "缺失必要元素", "成文日期")
        assert len(errs) == 1
        assert errs[0]["page_no"] == 2

    def test_no_zero_page_in_output(self) -> None:
        page = _make_page(0)
        page["page_content"]["body"]["title"] = None
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert all(e["page_no"] >= 1 for e in result["errors"])
        assert all(u["page_no"] is None or u["page_no"] >= 1 for u in result["unchecked"])


# ---------------------------------------------------------------------------
# DOCX 多页页码语义
# ---------------------------------------------------------------------------


class TestDocxPageNumberSemantics:
    def test_multipage_docx_page_number_only_on_first_page(self) -> None:
        """第 2 页起 footer.page_number 为 None 是"未携带"，不报缺页码错误。"""
        # DOCX 语义：版记内容（黑色反线/印发机关/印发日期）由正文流末尾分类到
        # 最后一个页组；page_number 只随第一个页组携带，第 2 页为 None。
        pages = [
            _make_page(0, first_page=True, last_page=False, with_page_number=True),
            _make_page(1, first_page=False, last_page=True),
        ]
        assert pages[1]["page_content"]["footer"].get("page_number") is None
        result = compare_document_to_spec(_make_doc(pages), _make_spec())
        assert _errors(result, "缺失必要元素", "页码") == []
        assert _errors(result, "缺失必要元素") == []

    def test_first_page_must_carry_page_number(self) -> None:
        """首页必须携带页码：首页 page_number 为 None 报错，page_no 为 1。"""
        page = _make_page(0, with_page_number=False)
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "缺失必要元素", "页码")
        assert len(errs) == 1
        assert errs[0]["page_no"] == 1

    def test_compliant_single_page_has_no_errors(self) -> None:
        result = compare_document_to_spec(_make_doc([_make_page(0)]), _make_spec())
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# 模板子包适配（死代码处置后）
# ---------------------------------------------------------------------------


class TestTemplatePackageAdaptation:
    def test_dead_functions_removed(self) -> None:
        """spec_to_sample_document / load_builtin_skeleton 零调用，已删除。"""
        import validator.templates as templates_pkg

        assert not hasattr(templates_pkg, "spec_to_sample_document")
        assert not hasattr(templates_pkg, "load_builtin_skeleton")

    def test_spec_from_template_content_kept(self) -> None:
        from validator.templates import spec_from_template_content

        content = {"version": "1.0", "page": {}, "elements": []}
        assert spec_from_template_content(content) is content
        with pytest.raises(ValueError, match="Unsupported template format"):
            spec_from_template_content({"legacy": {}})
        with pytest.raises(TypeError):
            spec_from_template_content("not a dict")


# ---------------------------------------------------------------------------
# 固定槽位块：一行混排格式的字体覆盖
# ---------------------------------------------------------------------------


def _logo_element(text: str, font_family: str, font_size: float, bold: bool) -> dict[str, Any]:
    return {
        "position": {"x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0},
        "font": {
            "font_family": font_family,
            "font_size": font_size,
            "font_weight": bold,
            "font_style": False,
            "text": text,
            "line_no": 0,
        },
    }


class TestFixedBlockFontCoverage:
    """固定槽位块（spec elements 恒为 1 个样板）的所有实际元素都要比对字体。

    _make_spec 的 issuing_logo 样板为仿宋 22pt 非加粗。
    """

    def test_mixed_run_violation_in_later_run_detected(self) -> None:
        """ "XX局文件"式混排：首个 run 合规、后续 run 违规也必须检出。"""
        runs = [
            _logo_element("XX县人民政府", "仿宋", 22.0, False),  # 合规
            _logo_element("文件", "黑体", 16.0, True),  # 字体/字号/字重均违规
        ]
        page = _make_page(0)
        page["page_content"]["header"]["issuing_logo"] = _para_dict("", elements=runs)
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        errs = _errors(result, "字体不匹配", "发文机关标志")
        assert len(errs) == 1
        assert "字体应为「仿宋」，实际为「黑体」" in errs[0]["details"]
        assert "字号应为 22" in errs[0]["details"]

    def test_mixed_run_all_compliant_no_error(self) -> None:
        """混排但每个 run 都符合规范 → 无字体错误。"""
        runs = [
            _logo_element("XX县人民政府", "仿宋", 22.0, False),
            _logo_element("文件", "FangSong", 22.0, False),  # 英文名归一化为仿宋
        ]
        page = _make_page(0)
        page["page_content"]["header"]["issuing_logo"] = _para_dict("", elements=runs)
        result = compare_document_to_spec(_make_doc([page]), _make_spec())
        assert _errors(result, "字体不匹配", "发文机关标志") == []


# ---------------------------------------------------------------------------
# PDF 多页页码语义
# ---------------------------------------------------------------------------


def _pdf_page_number(text: str, font_size: float) -> dict[str, Any]:
    """PDF 语义的页码段：带实测版面坐标（非 0 position）。"""
    return _para_dict(
        text,
        font_size=font_size,
        elements=[
            {
                "position": {"x0": 280.0, "y0": 780.0, "x1": 300.0, "y1": 794.0},
                "font": {
                    "font_family": "仿宋",
                    "font_size": font_size,
                    "font_weight": False,
                    "font_style": False,
                    "text": text,
                    "line_no": 0,
                },
            }
        ],
    )


def _make_pdf_two_page_doc(
    first_page_number_size: float | None,
    second_page_number_size: float | None,
) -> dict[str, Any]:
    """构造 2 页 PDF 语义文档：页码在每页的 footer 中实际存在（或按 None 缺席）。

    spec 的 footer.page_number 样版为仿宋 16pt（_make_spec 默认值）。
    """
    pages = [
        _make_page(0, first_page=True, last_page=False, with_page_number=False),
        _make_page(1, first_page=False, last_page=True, with_page_number=False),
    ]
    if first_page_number_size is not None:
        pages[0]["page_content"]["footer"]["page_number"] = _pdf_page_number(
            "— 1 —", first_page_number_size
        )
    if second_page_number_size is not None:
        pages[1]["page_content"]["footer"]["page_number"] = _pdf_page_number(
            "— 2 —", second_page_number_size
        )
    return _make_doc(pages, source="pdf")


class TestPdfPageNumberSemantics:
    """PDF 多页：页码在每一页都实际存在。should_exist_on_page 只控制"缺失是否
    报错"；已存在的 page_number 段在任何页都要比较字体/字号（修后语义）。"""

    def test_page_number_on_later_pages_font_checked(self) -> None:
        """第 2 页页码字号错误（14 ≠ 规范 16）→ 检出字体不匹配，page_no=2。"""
        doc = _make_pdf_two_page_doc(16.0, 14.0)
        result = compare_document_to_spec(doc, _make_spec())
        errs = _errors(result, "字体不匹配", "页码")
        assert len(errs) == 1
        assert errs[0]["page_no"] == 2
        assert "字号应为 16" in errs[0]["details"]

    def test_page_number_on_all_pages_compliant(self) -> None:
        """两页页码齐全且字体字号合规 → 页码块零错误。"""
        doc = _make_pdf_two_page_doc(16.0, 16.0)
        result = compare_document_to_spec(doc, _make_spec())
        assert [e for e in result["errors"] if e["block_name"] == "页码"] == []

    def test_first_page_page_number_still_required(self) -> None:
        """首页页码缺失仍报错（page_no=1）；第 2 页页码存在不误报缺失。"""
        doc = _make_pdf_two_page_doc(None, 16.0)
        result = compare_document_to_spec(doc, _make_spec())
        errs = _errors(result, "缺失必要元素", "页码")
        assert len(errs) == 1
        assert errs[0]["page_no"] == 1
