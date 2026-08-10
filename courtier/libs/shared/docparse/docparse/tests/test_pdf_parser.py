"""Tests for PdfParser: single-pass extraction, per-page fault tolerance,
position-based spacing estimation, and registry mixed/scanned dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import docparse.parsers.pdf_parser as pdf_parser_mod
import fitz
import pytest
from docmodels import (
    Body,
    Document,
    Font,
    Footer,
    Header,
    LineElement,
    Margin,
    Page,
    PageContent,
    Paragraph,
    Position,
)
from docparse.parsers import registry
from docparse.parsers.base import ParserConfig
from docparse.parsers.pdf_parser import PdfPageExtraction, PdfParser
from docparse.parsers.structure_recognizer import _estimate_spacing_from_positions


def _make_text_dict(text: str | None) -> dict:
    """Minimal get_text("dict") payload; text=None → page with no text blocks."""
    if text is None:
        return {"blocks": []}
    return {
        "blocks": [
            {
                "type": 0,
                "bbox": (72.0, 72.0, 300.0, 90.0),
                "lines": [
                    {
                        "bbox": (72.0, 72.0, 300.0, 90.0),
                        "spans": [{"text": text, "font": "SimSun", "size": 16.0}],
                    }
                ],
            }
        ]
    }


def _make_line(text: str = "正文内容") -> dict:
    return {
        "text": text,
        "line_no": 0,
        "x0": 72.0,
        "y0": 72.0,
        "x1": 300.0,
        "y1": 90.0,
        "font_family": "SimSun",
        "font_size": 16.0,
        "font_weight": False,
        "font_style": False,
    }


class _FakePage:
    def __init__(self, text_dict: dict | None = None, error: Exception | None = None):
        self._text_dict = text_dict
        self._error = error
        self.get_text_calls = 0
        self.rect = SimpleNamespace(width=595.28, height=841.89)

    def get_text(self, mode: str, flags: int = 0) -> dict:
        self.get_text_calls += 1
        assert mode == "dict"
        if self._error is not None:
            raise self._error
        return self._text_dict


class _FakeDoc:
    def __init__(self, pages: list[_FakePage]):
        self._pages = pages
        self.closed = False

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, idx: int) -> _FakePage:
        return self._pages[idx]

    def close(self) -> None:
        self.closed = True


def _install_fake_fitz(monkeypatch: pytest.MonkeyPatch, pages: list[_FakePage]) -> _FakeDoc:
    doc = _FakeDoc(pages)
    fake_fitz = SimpleNamespace(open=lambda _path: doc, TEXT_PRESERVE_WHITESPACE=8)
    monkeypatch.setattr(pdf_parser_mod, "fitz", fake_fitz)
    return doc


class _FakeScannedParser:
    """Records instantiation and parse calls; returns a sentinel Document."""

    instances: list[_FakeScannedParser] = []

    def __init__(self):
        self.parse_calls: list[tuple[str, object]] = []
        _FakeScannedParser.instances.append(self)

    def supports(self, file_path: str) -> bool:
        return True

    def parse(self, file_path: str, config=None) -> Document:
        self.parse_calls.append((file_path, config))
        return Document(doc_id="fake-scanned", total_page_num=0)


def _extraction(text_flags: list[bool]) -> PdfPageExtraction:
    pages = []
    for has_text in text_flags:
        pages.append({"lines": [_make_line()] if has_text else [], "margin": Margin()})
    return PdfPageExtraction(pages=pages)


@pytest.fixture
def pdf_file(tmp_path):
    f = tmp_path / "sample.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    return str(f)


@pytest.fixture(autouse=True)
def _reset_fake_scanned():
    _FakeScannedParser.instances.clear()
    yield
    _FakeScannedParser.instances.clear()


class TestSinglePassExtraction:
    def test_get_text_called_once_per_page(self, monkeypatch, pdf_file):
        pages = [_FakePage(_make_text_dict(f"第{i}页正文")) for i in range(3)]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        assert [p.get_text_calls for p in pages] == [1, 1, 1]
        assert result.total_page_num == 3
        assert len(result.pages) == 3

    def test_pages_carry_no_raw_or_save_path(self, monkeypatch, pdf_file):
        """Page.raw / Page.save_path were removed from the model."""
        pages = [_FakePage(_make_text_dict("正文内容"))]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        assert "raw" not in Page.model_fields
        assert "save_path" not in Page.model_fields
        assert result.pages[0].page_no == 0


class TestFaultTolerance:
    def test_single_page_failure_degrades_to_warning(self, monkeypatch, pdf_file):
        pages = [
            _FakePage(_make_text_dict("第一页正文")),
            _FakePage(error=RuntimeError("corrupt page")),
            _FakePage(_make_text_dict("第三页正文")),
        ]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        # One failing page must not abort the whole document
        assert len(result.pages) == 3
        assert len(result.warnings) == 1
        assert "第 2 页内容提取失败" in result.warnings[0]
        assert "corrupt page" in result.warnings[0]
        assert result.pages[1].page_content.body.main_text == []

    def test_all_pages_fail_raises(self, monkeypatch, pdf_file):
        pages = [_FakePage(error=RuntimeError("boom")) for _ in range(3)]
        _install_fake_fitz(monkeypatch, pages)

        with pytest.raises(RuntimeError, match="全部"):
            PdfParser().parse(pdf_file, ParserConfig())

    def test_textless_page_warns(self, monkeypatch, pdf_file):
        pages = [
            _FakePage(_make_text_dict("正文内容")),
            _FakePage(_make_text_dict(None)),
        ]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        assert any("第 2 页无文本层" in w for w in result.warnings)

    def test_failed_page_not_double_warned(self, monkeypatch, pdf_file):
        # Serial path (<=2 pages): page 1 fails, page 2 is textless
        pages = [
            _FakePage(error=RuntimeError("boom")),
            _FakePage(_make_text_dict(None)),
        ]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        assert len(result.warnings) == 2
        assert "第 1 页内容提取失败" in result.warnings[0]
        assert "第 2 页无文本层" in result.warnings[1]


class TestRegistryDispatch:
    """registry.parse detects textless pages once and reuses the extraction."""

    def test_all_text_reuses_extraction(self, monkeypatch, pdf_file):
        calls = []

        def fake_extract(path):
            calls.append(path)
            return _extraction([True, True])

        monkeypatch.setattr(pdf_parser_mod, "extract_pdf_pages", fake_extract)
        # PdfParser must not reopen the PDF when an extraction is supplied
        monkeypatch.setattr(
            pdf_parser_mod,
            "fitz",
            SimpleNamespace(
                open=lambda _p: pytest.fail("fitz.open called despite extraction"),
                TEXT_PRESERVE_WHITESPACE=8,
            ),
        )

        result = registry.parse(pdf_file, ParserConfig())

        assert calls == [pdf_file]
        assert result.total_page_num == 2
        assert len(result.pages) == 2
        assert result.warnings == []
        assert result.source == "pdf"

    def test_mixed_with_ocr_goes_scanned(self, monkeypatch, pdf_file):
        import docparse.parsers.scanned as scanned_mod

        monkeypatch.setattr(
            pdf_parser_mod, "extract_pdf_pages", lambda _p: _extraction([True, False])
        )
        monkeypatch.setattr(scanned_mod, "ScannedParser", _FakeScannedParser)

        result = registry.parse(pdf_file, ParserConfig(llm_api_key="test-key"))

        assert result.doc_id == "fake-scanned"
        # 混排走 ScannedParser，source 记 "scanned"
        assert result.source == "scanned"
        assert len(_FakeScannedParser.instances) == 1
        assert _FakeScannedParser.instances[0].parse_calls[0][0] == pdf_file

    def test_mixed_without_ocr_uses_pdf_and_lists_pages(self, monkeypatch, pdf_file):
        import docparse.parsers.scanned as scanned_mod

        monkeypatch.setattr(
            pdf_parser_mod,
            "extract_pdf_pages",
            lambda _p: _extraction([True, False, True]),
        )
        monkeypatch.setattr(scanned_mod, "ScannedParser", _FakeScannedParser)

        result = registry.parse(pdf_file, ParserConfig())

        assert _FakeScannedParser.instances == []
        assert result.total_page_num == 3
        assert any("第 2 页无文本层" in w for w in result.warnings)
        assert result.source == "pdf"

    def test_all_scanned_goes_scanned(self, monkeypatch, pdf_file):
        import docparse.parsers.scanned as scanned_mod

        monkeypatch.setattr(
            pdf_parser_mod, "extract_pdf_pages", lambda _p: _extraction([False, False])
        )
        monkeypatch.setattr(scanned_mod, "ScannedParser", _FakeScannedParser)

        result = registry.parse(pdf_file, ParserConfig())

        assert result.doc_id == "fake-scanned"
        assert result.source == "scanned"
        assert len(_FakeScannedParser.instances) == 1

    def test_get_parser_mixed_dispatch(self, monkeypatch, tmp_path):
        from docparse.parsers.scanned import ScannedParser

        f = tmp_path / "mixed.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(
            pdf_parser_mod, "extract_pdf_pages", lambda _p: _extraction([True, False])
        )

        with_ocr = registry.get_parser(str(f), ParserConfig(llm_api_key="test-key"))
        without_ocr = registry.get_parser(str(f), ParserConfig())

        assert isinstance(with_ocr, ScannedParser)
        assert isinstance(without_ocr, PdfParser)

    def test_get_parser_unprobed_pdf_falls_back_to_pdf_parser(self, monkeypatch, tmp_path):
        f = tmp_path / "broken.pdf"
        f.write_bytes(b"garbage")

        def boom(_path):
            raise RuntimeError("cannot open")

        monkeypatch.setattr(pdf_parser_mod, "extract_pdf_pages", boom)

        assert isinstance(registry.get_parser(str(f)), PdfParser)


def _multi_line_text_dict() -> dict:
    """One text block with two body lines 30pt apart (top-to-top)."""
    return {
        "blocks": [
            {
                "type": 0,
                "bbox": (72.0, 100.0, 500.0, 150.0),
                "lines": [
                    {
                        "bbox": (72.0, 100.0, 500.0, 120.0),
                        "spans": [
                            {
                                "text": "为贯彻落实上级决策部署扎实推进各项工作有序开展",
                                "font": "SimSun",
                                "size": 16.0,
                            }
                        ],
                    },
                    {
                        "bbox": (72.0, 130.0, 500.0, 150.0),
                        "spans": [
                            {
                                "text": "各部门单位要高度重视切实履行主体责任确保到位",
                                "font": "SimSun",
                                "size": 16.0,
                            }
                        ],
                    },
                ],
            }
        ]
    }


class TestSpacingEstimation:
    """_estimate_spacing_from_positions (PDF path, pt units)."""

    def _para(self, ys: list[tuple[float, float]], outline: str = "body_text") -> Paragraph:
        elements = [
            LineElement(
                position=Position(x0=72.0, y0=y0, x1=300.0, y1=y1),
                font=Font(text="行", line_no=i),
            )
            for i, (y0, y1) in enumerate(ys)
        ]
        return Paragraph(elements=elements, outline_level=outline)

    def test_line_spacing_is_median_top_diff(self):
        # Upper-median convention (sorted[len//2]), same as the rest of
        # docparse: diffs are [30, 35] → median 35.
        para = self._para([(100.0, 120.0), (130.0, 150.0), (165.0, 185.0)])
        pc = PageContent(body=Body(main_text=[para]))

        _estimate_spacing_from_positions(pc)

        assert para.line_spacing == 35.0

    def test_compliant_document_estimates_zero_spacing(self):
        """合规 PDF（28.95pt 固定行距、行盒高 20pt、真实段距 0）：
        段前一律 None（单侧记账），段后净间距 ≈ 0（±1pt）。"""
        # Every line-to-line top distance is 28.95pt; box height 20pt →
        # normal line gap 8.95pt everywhere, no real paragraph spacing.
        title = self._para([(100.0, 120.0)], outline="heading1")
        first = self._para([(128.95, 148.95), (157.9, 177.9)])
        second = self._para([(186.85, 206.85), (215.8, 235.8)])
        pc = PageContent(body=Body(title=title, main_text=[first, second]))

        _estimate_spacing_from_positions(pc)

        for para in (title, first, second):
            assert para.space_before is None
        assert title.space_after == pytest.approx(0.0, abs=1.0)
        assert first.space_after == pytest.approx(0.0, abs=1.0)
        assert second.space_after is None  # last paragraph of the region

    def test_real_paragraph_spacing_detected(self):
        """带真实段距（15pt）的边界能检出，且只记在上一段的段后。"""
        first = self._para([(128.95, 148.95), (157.9, 177.9)])
        # 15pt real spacing: tops shifted down by 15 vs. the 28.95 rhythm
        second = self._para([(201.85, 221.85), (230.8, 250.8)])
        pc = PageContent(body=Body(main_text=[first, second]))

        _estimate_spacing_from_positions(pc)

        # raw gap 23.95 − normal line gap 8.95 = net 15pt
        assert first.space_after == pytest.approx(15.0, abs=0.5)
        # 双间隙不双记：下一段的 space_before 留 None
        assert second.space_before is None
        assert second.space_after is None

    def test_no_spacing_booked_across_regions(self):
        """区域（header/body/footer）交界处不记间距：跨区域 y 距离是
        页面布局余白，不是段距。"""
        header_para = self._para([(60.0, 80.0)])
        title = self._para([(300.0, 320.0)], outline="heading1")
        body_para = self._para([(328.95, 348.95), (357.9, 377.9)])
        footer_para = self._para([(700.0, 720.0)])
        pc = PageContent(
            header=Header(issuing_number=header_para),
            body=Body(title=title, main_text=[body_para]),
            footer=Footer(page_number=footer_para),
        )

        _estimate_spacing_from_positions(pc)

        # header/footer 各自只有一段，区域内无相邻段落 → 不留值
        assert header_para.space_before is None
        assert header_para.space_after is None
        assert footer_para.space_before is None
        assert footer_para.space_after is None
        # body 内部仍正常估算：title → 正文段净间距 ≈ 0
        assert title.space_before is None
        assert title.space_after == pytest.approx(0.0, abs=1.0)
        assert body_para.space_before is None
        assert body_para.space_after is None

    def test_unestimatable_values_stay_none(self):
        para = self._para([(100.0, 120.0)])
        pc = PageContent(body=Body(main_text=[para]))

        _estimate_spacing_from_positions(pc)

        assert para.line_spacing is None
        assert para.space_before is None
        assert para.space_after is None

    def test_single_line_page_falls_back_to_doc_typical_gap(self):
        """页内无多行段：space_after 的行隙扣减回退文档级中位数。"""
        first = self._para([(100.0, 120.0)])
        second = self._para([(150.0, 170.0)])  # 段间净隙 30pt
        pc = PageContent(body=Body(main_text=[first, second]))

        _estimate_spacing_from_positions(pc, doc_typical_gap=10.0)

        assert first.space_after == pytest.approx(20.0, abs=0.01)
        assert second.space_after is None

    def test_single_line_page_without_doc_gap_keeps_raw_gap(self):
        """全文也无多行段（doc_typical_gap=None）：保持旧的 0.0 回退。"""
        first = self._para([(100.0, 120.0)])
        second = self._para([(150.0, 170.0)])
        pc = PageContent(body=Body(main_text=[first, second]))

        _estimate_spacing_from_positions(pc)

        assert first.space_after == pytest.approx(30.0, abs=0.01)

    def test_empty_page_content_noop(self):
        pc = PageContent()
        _estimate_spacing_from_positions(pc)  # must not raise

    def test_parse_estimates_line_spacing(self, monkeypatch, pdf_file):
        """End-to-end: two merged body lines yield a 30pt line spacing."""
        pages = [_FakePage(_multi_line_text_dict())]
        _install_fake_fitz(monkeypatch, pages)

        result = PdfParser().parse(pdf_file, ParserConfig())

        main_text = result.pages[0].page_content.body.main_text
        assert main_text, "expected at least one body paragraph"
        # Lines 10pt apart (bottom-to-top) merge into one paragraph;
        # top-to-top distance is 30pt.
        assert main_text[0].line_spacing == 30.0


# ---------------------------------------------------------------------------
# 真实 PDF 合成（fitz）：探针实测行高后按目标 y0 排版，保证段间距测量值
# 确定性（与 validator/tests/test_e2e_compliant.py 同一手法）。
# ---------------------------------------------------------------------------

_A4_W, _A4_H = 595.28, 841.89
_LEFT_PT = 79.37  # 左边距 28mm


def _probe_line_metrics() -> dict[int, tuple[float, float]]:
    """实测各字号行 bbox：{size: (baseline→y0 距离, 行高)}。"""
    probes = [(22, 120.0), (16, 220.0), (14, 320.0)]
    doc = fitz.open()
    page = doc.new_page(width=_A4_W, height=_A4_H)
    for size, baseline in probes:
        page.insert_text((_LEFT_PT, baseline), "国", fontname="china-s", fontsize=size)
    base_by_size = {size: base for size, base in probes}
    metrics: dict[int, tuple[float, float]] = {}
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                size = int(round(span["size"]))
                y0, y1 = line["bbox"][1], line["bbox"][3]
                metrics[size] = (base_by_size[size] - y0, y1 - y0)
    doc.close()
    assert len(metrics) == 3, f"探针行实测失败：{metrics}"
    return metrics


@pytest.fixture(scope="module")
def pdf_metrics() -> dict[int, tuple[float, float]]:
    return _probe_line_metrics()


class _LineCursor:
    """顺序排版游标：gap_before 为上一行 y1 到本行 y0 的净距离（pt）。"""

    def __init__(self, metrics: dict[int, tuple[float, float]]):
        self._metrics = metrics
        self.lines: list[tuple[str, float, float]] = []
        self._prev_y1 = 0.0

    def start(self, first_y0: float) -> None:
        self._prev_y1 = first_y0

    def add(self, text: str, size: int, gap_before: float) -> None:
        y0 = self._prev_y1 + gap_before
        self.lines.append((text, float(size), y0))
        self._prev_y1 = y0 + self._metrics[size][1]


def _write_pdf(
    path,
    pages_lines: list[list[tuple[str, float, float]]],
    metrics: dict[int, tuple[float, float]],
) -> None:
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page(width=_A4_W, height=_A4_H)
        for text, size, y0 in lines:
            ascent, _ = metrics[int(size)]
            page.insert_text((_LEFT_PT, y0 + ascent), text, fontname="china-s", fontsize=size)
    doc.save(str(path))
    doc.close()


class TestDocLevelLineGapFallback:
    """全单行段页的行隙回退：文档级段内行隙中位数（而非 0.0）。"""

    def test_single_line_paragraph_page_uses_doc_median(
        self, tmp_path, pdf_metrics: dict[int, tuple[float, float]]
    ) -> None:
        """合成两页 PDF：第 1 页含多行段，第 2 页全单行段。

        第 1 页段内行隙 = 30 − h16（top-to-top 30pt）；第 2 页段间净隙
        = 60 − h16。修后第 2 页 space_after = (60 − h16) − (30 − h16)
        = 30pt；修前回退 0.0（不扣行隙）→ ≈ 40pt，space_after 偏高。
        """
        h16 = pdf_metrics[16][1]

        page1 = _LineCursor(pdf_metrics)
        page1.start(100.0)
        for i in range(3):
            page1.add(
                f"第一页正文内容第{i + 1}行，用于构造多行段落排版。",
                16,
                (30.0 - h16) if i else 0.0,
            )

        page2 = _LineCursor(pdf_metrics)
        page2.start(100.0)
        for i in range(3):
            page2.add(
                f"第二页落款内容第{i + 1}行，单行各自成段排版。",
                16,
                (60.0 - h16) if i else 0.0,
            )

        path = tmp_path / "two_page.pdf"
        _write_pdf(path, [page1.lines, page2.lines], pdf_metrics)

        result = PdfParser().parse(str(path), ParserConfig())

        main_text = result.pages[1].page_content.body.main_text
        assert len(main_text) == 3, "第 2 页应为三个单行段（不被合并）"
        assert main_text[0].space_after == pytest.approx(30.0, abs=1.0)
        assert main_text[1].space_after == pytest.approx(30.0, abs=1.0)
        assert main_text[2].space_after is None  # 区域内末段


class TestRegionSplitBoundaryCases:
    """region split 两个已确诊边界误分类场景（合成 PDF 走真实解析链路）。"""

    def test_issue_date_above_footer_gap_stays_in_body(
        self, tmp_path, pdf_metrics: dict[int, tuple[float, float]]
    ) -> None:
        """场景 a：成文日期紧贴版记空隙，不得被划进 footer。

        版式：正文末行（y≥70% 页高）→ 60pt 空隙 → 成文日期 → 35pt 空隙
        → 印发机关行。修前最大空隙（60pt）被选为版记边界，日期落入
        footer 多认一个 distribution_date；修后边界下移到「日期→版记」，
        日期留在主体识别为 issue_date。
        """
        h16 = pdf_metrics[16][1]
        g16 = 28.95 - h16  # 28.95pt 固定行距下的段内行隙

        page1 = _LineCursor(pdf_metrics)
        page1.start(105.0)
        page1.add("测试县人民政府文件", 22, 0.0)
        page1.add("测政发〔2026〕1号", 16, g16 + 6.0)
        page1.add("关于开展测试工作的通知", 22, 40.0)
        page1.add("各乡镇人民政府：", 16, g16 + 6.0)
        for i in range(13):  # 13 行正文，把末行推过 0.7 页高
            page1.add(f"正文内容第{i + 1}行，用于测试固定行距排版。", 16, g16)
        page1.add("2026年5月11日", 16, 60.0)  # 成文日期紧贴版记空隙
        page1.add("测试县人民政府办公室2026年5月12日印发", 14, 35.0)
        page1.add("1", 14, 10.0)

        path = tmp_path / "date_above_footer_gap.pdf"
        _write_pdf(path, [page1.lines], pdf_metrics)

        result = PdfParser().parse(str(path), ParserConfig())
        pc = result.pages[0].page_content

        # 成文日期归主体；印发日期只来自印发机关行（1 个元素）
        assert pc.body.issue_date is not None
        assert "2026年5月11日" in pc.body.issue_date.elements[0].font.text
        assert pc.footer.distribution_date is not None
        assert len(pc.footer.distribution_date.elements) == 1
        dist_text = pc.footer.distribution_date.elements[0].font.text
        assert "2026年5月12日" in dist_text
        assert "2026年5月11日" not in dist_text

    def test_short_continuation_page_body_not_unclassified(
        self, tmp_path, pdf_metrics: dict[int, tuple[float, float]]
    ) -> None:
        """场景 b：短第 2 页的「日期→版记」空隙不得被当成版头/主体边界。

        第 2 页只有 2 行正文 + 署名 + 成文日期（均在中点之上），随后大
        空隙到底部版记。修前该空隙被选为版头边界，正文行划入 header 后
        全部落入 unclassified 兜底；修后版头边界因无版头标志被拒绝，
        正文行留在主体。
        """
        h16 = pdf_metrics[16][1]
        g16 = 28.95 - h16

        page1 = _LineCursor(pdf_metrics)
        page1.start(105.0)
        page1.add("测试县人民政府文件", 22, 0.0)
        page1.add("测政发〔2026〕1号", 16, g16 + 6.0)
        page1.add("关于开展测试工作的通知", 22, 40.0)
        page1.add("各乡镇人民政府：", 16, g16 + 6.0)
        for i in range(12):
            page1.add(f"正文内容第{i + 1}行，用于测试固定行距排版。", 16, g16)

        page2 = _LineCursor(pdf_metrics)
        page2.start(105.0)
        for i in range(2):
            page2.add(f"后续正文内容第{i + 1}行，保持固定行距排版。", 16, g16 if i else 0.0)
        page2.add("测试县人民政府", 16, g16)
        page2.add("2026年5月11日", 16, g16)
        page2.add("测试县人民政府办公室2026年5月12日印发", 14, 520.0)
        page2.add("2", 14, 10.0)

        path = tmp_path / "short_continuation_page.pdf"
        _write_pdf(path, [page1.lines, page2.lines], pdf_metrics)

        result = PdfParser().parse(str(path), ParserConfig())
        pc = result.pages[1].page_content

        # 正文行进入主体（unclassified 兜底也会保留文本，判别点是
        # 成文日期归主体：修前它落入 header 区 → issue_date=None）
        main_text = "".join(e.font.text for para in pc.body.main_text for e in para.elements)
        assert "后续正文内容第1行" in main_text
        assert pc.body.issue_date is not None
        assert "2026年5月11日" in pc.body.issue_date.elements[0].font.text
        # 版记仍由关键词兜底识别
        assert pc.footer.issuing_office is not None


class TestUnclassifiedWarningsSurfaced:
    """规则引擎未归类行的摘要应进入 Document.warnings（修前只进日志）。"""

    def test_pdf_unclassified_lines_in_document_warnings(
        self, tmp_path, monkeypatch, pdf_metrics: dict[int, tuple[float, float]]
    ) -> None:
        from docparse.parsers.rules import ClassifiedLine

        cursor = _LineCursor(pdf_metrics)
        cursor.start(100.0)
        cursor.add("一行用于测试警告透传的普通正文内容。", 16, 0.0)
        path = tmp_path / "unclassified.pdf"
        _write_pdf(path, [cursor.lines], pdf_metrics)

        class _StubEngine:
            """全部行强制 unclassified，隔离规则引擎启发式的不确定性。"""

            def classify_lines(self, lines, has_position, page_height):
                return SimpleNamespace(
                    lines=[
                        ClassifiedLine(
                            line_no=i,
                            text=line.get("text", ""),
                            field="unclassified",
                            confidence=0.0,
                        )
                        for i, line in enumerate(lines)
                    ]
                )

        monkeypatch.setattr(pdf_parser_mod, "StructureRuleEngine", _StubEngine)

        doc = PdfParser().parse(str(path), ParserConfig())

        assert any(
            w.startswith("第 1 页：") and "未能自动归类" in w for w in doc.warnings
        ), f"未归类摘要应进入 Document.warnings：{doc.warnings}"
