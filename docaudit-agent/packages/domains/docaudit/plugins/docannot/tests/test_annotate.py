from pathlib import Path

import pytest
from docxnote import DocxDocument

from docannot import Rule, annotate


SAMPLE_DOCX = Path("tests/assets/doc.docx")


@pytest.fixture
def sample_docx() -> bytes:
    """读取测试用 DOCX 文件。"""
    if not SAMPLE_DOCX.exists():
        pytest.skip("Test asset missing")
    return SAMPLE_DOCX.read_bytes()


class TestAnnotate:
    def test_annotate_with_bytes_input(self, sample_docx: bytes) -> None:
        rules = [Rule(keyword="落实", comment="请检查", author="校对员")]
        result = annotate(sample_docx, rules)
        assert isinstance(result, bytes)
        assert len(result) > 0

        doc = DocxDocument.parse(result, keep_comments=True)
        comments = doc.comments()
        assert any(c.text == "请检查" for c in comments)

    def test_annotate_with_path_input(self) -> None:
        if not SAMPLE_DOCX.exists():
            pytest.skip("Test asset missing")
        rules = [Rule(keyword="落实", comment="请检查")]
        result = annotate(SAMPLE_DOCX, rules)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_annotate_with_str_input(self) -> None:
        if not SAMPLE_DOCX.exists():
            pytest.skip("Test asset missing")
        rules = [Rule(keyword="落实", comment="请检查")]
        result = annotate(str(SAMPLE_DOCX), rules)
        assert isinstance(result, bytes)

    def test_keep_comments_false(self, sample_docx: bytes) -> None:
        rules = [Rule(keyword="落实", comment="新批注")]
        result = annotate(sample_docx, rules, keep_comments=False)
        doc = DocxDocument.parse(result, keep_comments=True)
        comments = doc.comments()
        assert len(comments) >= 1
        assert any(c.text == "新批注" for c in comments)

    def test_multiple_rules(self, sample_docx: bytes) -> None:
        rules = [
            Rule(keyword="落实", comment="检查落实"),
            Rule(keyword="工作", comment="检查工作"),
        ]
        result = annotate(sample_docx, rules)
        doc = DocxDocument.parse(result, keep_comments=True)
        comments = doc.comments()
        assert any(c.text == "检查落实" for c in comments)

    def test_no_match_returns_valid_docx(self, sample_docx: bytes) -> None:
        rules = [Rule(keyword="不可能匹配的关键词xyz", comment="批注")]
        result = annotate(sample_docx, rules)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_empty_rules_returns_docx(self, sample_docx: bytes) -> None:
        result = annotate(sample_docx, [])
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_comment_range_is_precise(self, sample_docx: bytes) -> None:
        """批注范围应精确覆盖关键词，而非整个段落。"""
        rules = [Rule(keyword="落实", comment="请检查", author="校对员")]
        result = annotate(sample_docx, rules)
        doc = DocxDocument.parse(result, keep_comments=True)
        for c in doc.comments():
            highlighted = c.paragraph.text[c.start : c.end]
            assert highlighted == "落实", (
                f"批注范围应精确为'落实'，实际为'{highlighted}' "
                f"(start={c.start}, end={c.end})"
            )
