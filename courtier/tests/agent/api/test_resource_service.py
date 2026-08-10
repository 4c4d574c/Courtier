"""Unit tests for resource library text extraction and chunking."""

from __future__ import annotations

from courtier.agent.api.services.resource_service import _extract_text, _split_chunks


class TestSplitChunks:
    def test_short_text_single_chunk(self):
        assert _split_chunks("第一段。\n第二段。") == ["第一段。\n第二段。"]

    def test_paragraphs_grouped_up_to_size(self):
        paras = [f"第{i}段" + "字" * 90 for i in range(10)]  # ~94 chars each
        chunks = _split_chunks("\n".join(paras), chunk_size=300)
        assert all(len(c) <= 300 for c in chunks)
        assert len(chunks) > 1
        # No paragraph content lost
        assert "".join(chunks).count("第") == 10

    def test_oversized_paragraph_hard_split(self):
        long_para = "长" * 2500
        chunks = _split_chunks(long_para, chunk_size=1000)
        assert chunks == ["长" * 1000, "长" * 1000, "长" * 500]

    def test_empty_and_blank_paragraphs_ignored(self):
        assert _split_chunks("\n\n  \n\n") == []

    def test_mixed_short_and_long_paragraphs(self):
        text = "短段。\n" + "长" * 1200 + "\n结尾。"
        chunks = _split_chunks(text, chunk_size=500)
        assert chunks[0] == "短段。"
        assert chunks[1] == "长" * 500
        assert chunks[2] == "长" * 500
        assert chunks[3] == "长" * 200 + "\n结尾。" or chunks[-1].endswith("结尾。")


class TestExtractText:
    def test_txt_file(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("纯文本内容\n第二行", encoding="utf-8")
        assert _extract_text(p) == "纯文本内容\n第二行"

    def test_md_file(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("# 标题\n正文", encoding="utf-8")
        assert "标题" in _extract_text(p)

    def test_docx_file(self, tmp_path):
        import docx

        p = tmp_path / "a.docx"
        doc = docx.Document()
        doc.add_paragraph("第一段内容")
        doc.add_paragraph("第二段内容")
        doc.save(str(p))
        text = _extract_text(p)
        assert "第一段内容" in text
        assert "第二段内容" in text

    def test_pdf_file(self, tmp_path):
        import fitz

        p = tmp_path / "a.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "PDF 测试文本")
        doc.save(str(p))
        doc.close()
        assert "PDF" in _extract_text(p)
