"""Tests for scanned-pipeline spacing semantics (scanned/spacing.py).

Covers the None-vs-0.0 contract: unmeasurable values (space_before of
scanned lines, space_after of a page's last line) are None (未测得),
never a hardcoded 0.0; and paragraph-level space_after is taken from
the paragraph's LAST line.
"""

from __future__ import annotations

from docmodels import Body, Font, LineElement, Margin, PageContent, Paragraph, Position
from docparse.parsers.scanned.font_detector import (
    _estimate_available_width_mm,
    refine_font_size_by_chars_per_line,
)
from docparse.parsers.scanned.spacing import (
    merge_spacing_into_page_content,
    normalize_body_line_spacing,
)


def _line(line_no: int, y0: float, y1: float, text: str = "行") -> LineElement:
    return LineElement(
        position=Position(x0=72.0, y0=y0, x1=300.0, y1=y1),
        font=Font(text=text, line_no=line_no),
    )


def _spacing(space_after: float | None, line_spacing: float = 26.78) -> dict:
    return {
        "space_before": None,
        "space_after": space_after,
        "line_spacing": line_spacing,
    }


class TestMergeSpacingIntoPageContent:
    def test_space_after_taken_from_last_line(self):
        """多行段的段后记在末行：space_after 取末行查表值，而非首行。"""
        para = Paragraph(
            elements=[_line(0, 100.0, 120.0), _line(1, 130.0, 150.0), _line(2, 280.0, 300.0)],
            outline_level="body_text",
        )
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {
            0: _spacing(0.0),  # 首行：非段尾，space_after=0.0（段内行）
            1: _spacing(0.0),
            2: _spacing(7.5),  # 末行：段后实测 7.5pt
        }

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_after == 7.5
        # space_before / line_spacing 仍取首行
        assert para.space_before is None
        assert para.line_spacing == 26.78

    def test_first_line_space_after_not_used_for_multiline_para(self):
        """首行携带的 space_after 不会盖到多行段上。"""
        para = Paragraph(
            elements=[_line(0, 100.0, 120.0), _line(1, 130.0, 150.0)],
            outline_level="body_text",
        )
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(99.0), 1: _spacing(3.0)}

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_after == 3.0

    def test_single_line_paragraph_uses_same_line(self):
        para = Paragraph(elements=[_line(0, 100.0, 120.0)], outline_level="body_text")
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(4.0)}

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_before is None
        assert para.space_after == 4.0

    def test_last_line_picked_by_y0_not_element_order(self):
        """末行按 y0 判定，对乱序 line_indices 稳健。"""
        para = Paragraph(
            elements=[_line(2, 280.0, 300.0), _line(0, 100.0, 120.0), _line(1, 130.0, 150.0)],
            outline_level="body_text",
        )
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(0.0), 1: _spacing(0.0), 2: _spacing(6.0)}

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_after == 6.0

    def test_implausible_space_after_reset_to_none(self):
        """段后超过语义上限（100pt）的是页面布局空白，置 None。"""
        para = Paragraph(elements=[_line(0, 100.0, 120.0)], outline_level="others")
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(313.1)}

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_after is None

    def test_plausible_space_after_kept(self):
        para = Paragraph(elements=[_line(0, 100.0, 120.0)], outline_level="others")
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(100.0)}

        merge_spacing_into_page_content(pc, spacing_map, {})

        assert para.space_after == 100.0

    def test_synthetic_line_paragraph_not_merged(self):
        """合成线要素（line_no=-1，无源文本行）不参与间距/缩进查表。"""
        para = Paragraph(
            elements=[
                LineElement(
                    position=Position(x0=72.0, y0=898.0, x1=1137.0, y1=948.0),
                    font=Font(text="", line_no=-1),
                )
            ],
        )
        pc = PageContent(body=Body(main_text=[para]))
        spacing_map = {0: _spacing(172.92)}
        indent_map = {0: 32.0}
        lr_map = {0: {"left_indent": 10.0, "right_indent": 5.0}}

        merge_spacing_into_page_content(pc, spacing_map, indent_map, lr_map)

        assert para.space_before is None
        assert para.space_after is None
        assert para.line_spacing is None
        assert para.first_indent is None
        assert para.left_indent is None
        assert para.right_indent is None

    def test_center_alignment_clears_indents(self):
        """居中段落的首行/左右缩进无排版语义（实测值只是居中位置），置 None。"""
        para = Paragraph(
            elements=[_line(0, 100.0, 120.0)],
            outline_level="others",
            alignment="center",
        )
        pc = PageContent(body=Body(main_text=[para]))
        indent_map = {0: 195.96}
        lr_map = {0: {"left_indent": 199.5, "right_indent": 0.0}}

        merge_spacing_into_page_content(pc, {}, indent_map, lr_map)

        assert para.first_indent is None
        assert para.left_indent is None
        assert para.right_indent is None

    def test_right_alignment_clears_indents(self):
        para = Paragraph(
            elements=[_line(0, 100.0, 120.0)],
            outline_level="others",
            alignment="right",
        )
        pc = PageContent(body=Body(main_text=[para]))
        indent_map = {0: 32.0}
        lr_map = {0: {"left_indent": 320.0, "right_indent": 0.0}}

        merge_spacing_into_page_content(pc, {}, indent_map, lr_map)

        assert para.first_indent is None
        assert para.left_indent is None
        assert para.right_indent is None

    def test_left_alignment_keeps_indents(self):
        para = Paragraph(
            elements=[_line(0, 100.0, 120.0)],
            outline_level="body_text",
            alignment="left",
        )
        pc = PageContent(body=Body(main_text=[para]))
        indent_map = {0: 32.0}
        lr_map = {0: {"left_indent": 16.0, "right_indent": 0.0}}

        merge_spacing_into_page_content(pc, {}, indent_map, lr_map)

        assert para.first_indent == 32.0
        assert para.left_indent == 16.0
        assert para.right_indent == 0.0


class TestEstimateAvailableWidthMm:
    def test_plausible_measured_margins_used(self):
        """实测边距在合理区间时用实测版心（如 22.5/17.2mm）。"""
        pm = [{"margin": Margin(left_margin=22.5, right_margin=17.2)}]
        assert _estimate_available_width_mm(pm) == 210.0 - 22.5 - 17.2

    def test_cropped_scan_falls_back_to_standard(self):
        """贴边扫描（边距≈0）回退 GB/T 标准版心 156mm。"""
        pm = [{"margin": Margin(left_margin=0.0, right_margin=0.0)}]
        assert _estimate_available_width_mm(pm) == 210.0 - 28.0 - 26.0

    def test_missing_margin_falls_back(self):
        assert _estimate_available_width_mm([{}]) == 156.0

    def test_sides_independent(self):
        """单侧可信时只替换该侧。"""
        pm = [{"margin": Margin(left_margin=22.5, right_margin=3.0)}]
        assert _estimate_available_width_mm(pm) == 210.0 - 22.5 - 26.0


class TestRefineFontSizeByCharsPerLine:
    def _page_metrics(self, left_mm: float, right_mm: float) -> list[dict]:
        # 30 个非缩进正文行，每行 30 字
        lines = [
            {"text": "正" * 30, "outline_level": "body_text", "font_size": 0.0} for _ in range(30)
        ]
        return [
            {
                "lines": lines,
                "margin": Margin(left_margin=left_mm, right_margin=right_mm),
                "indent_map": {},
            }
        ]

    def test_measured_margins_change_estimate(self):
        """窄边距文档（22.5/17.2mm）版心更宽，估出 16pt 而非 15pt。"""
        page_metrics = self._page_metrics(22.5, 17.2)

        refine_font_size_by_chars_per_line(page_metrics)

        assert page_metrics[0]["lines"][0]["font_size"] == 16.0

    def test_standard_margins_estimate(self):
        page_metrics = self._page_metrics(28.0, 26.0)

        refine_font_size_by_chars_per_line(page_metrics)

        assert page_metrics[0]["lines"][0]["font_size"] == 15.0


class TestNormalizeBodyLineSpacing:
    def _page_metrics(self) -> list[dict]:
        lines_p0 = [
            {"text": "一", "y0": 100.0, "y1": 120.0},
            {"text": "二", "y0": 130.0, "y1": 150.0},
            {"text": "三", "y0": 280.0, "y1": 300.0},
        ]
        lines_p1 = [
            {"text": "四", "y0": 100.0, "y1": 120.0},
            {"text": "五", "y0": 130.0, "y1": 150.0},
        ]
        return [
            {
                "lines": lines_p0,
                "spacing_map": {
                    0: _spacing(0.0),
                    1: _spacing(115.0),
                    2: _spacing(0.0),
                },
                "img_height": 943,
                "a4_height_pt": 841.89,
            },
            {
                "lines": lines_p1,
                "spacing_map": {0: _spacing(0.0), 1: _spacing(0.0)},
                "img_height": 943,
                "a4_height_pt": 841.89,
            },
        ]

    def test_page_last_line_space_after_is_none(self):
        """页末行 space_after 改 None（无下一行可测），且不回填 0.0。"""
        page_metrics = self._page_metrics()

        normalize_body_line_spacing(page_metrics)

        # 两页的页末行（page0 line 2、page1 line 1）都是 None
        assert page_metrics[0]["spacing_map"][2]["space_after"] is None
        assert page_metrics[1]["spacing_map"][1]["space_after"] is None

    def test_space_before_set_to_none(self):
        page_metrics = self._page_metrics()

        normalize_body_line_spacing(page_metrics)

        for pm in page_metrics:
            for info in pm["spacing_map"].values():
                assert info["space_before"] is None

    def test_break_reclassified_and_line_spacing_unified(self):
        """段间距按文档级阈值重分类；行距全文档统一。"""
        page_metrics = self._page_metrics()

        normalize_body_line_spacing(page_metrics)

        # page0 line 1 后是大间隙（130px）→ 段断，space_after > 0
        assert page_metrics[0]["spacing_map"][1]["space_after"] > 0.0
        # 行距统一到文档级取值
        line_spacings = {
            info["line_spacing"] for pm in page_metrics for info in pm["spacing_map"].values()
        }
        assert len(line_spacings) == 1
        assert line_spacings.pop() > 0.0

    def test_second_to_last_line_space_after_not_clobbered(self):
        """页末前一行（旧实现误当"页末行"清零的对象）的段断值保留。"""
        page_metrics = self._page_metrics()

        normalize_body_line_spacing(page_metrics)

        # page0 line 1 正是"页末前一行"：130px 大间隙段断值必须保留
        assert page_metrics[0]["spacing_map"][1]["space_after"] > 0.0
        # page1 line 0 是 10px 小间隙（非段断）→ 计算的 0.0 保持
        assert page_metrics[1]["spacing_map"][0]["space_after"] == 0.0
