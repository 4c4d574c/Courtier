import pytest

from validator.wenzhong import detect_wenzhong, detect_wenzhong_from_doc


class TestDetectWenzhongByTitle:
    """标题正则匹配测试。"""

    @pytest.mark.parametrize(
        "title, expected",
        [
            ("国务院关于印发XX规划的通知", "通知"),
            ("XX省人民政府关于XX的函", "函"),
            ("XX厅关于XX的请示", "请示"),
            ("XX部关于XX的报告", "报告"),
            ("XX省人民政府关于XX的批复", "批复"),
            ("XX部关于XX的通报", "通报"),
            ("全国人大关于XX的决议", "决议"),
            ("国务院关于XX的决定", "决定"),
            ("中华人民共和国主席令", "命令(令)"),
            ("XX会议纪要", "纪要"),
            ("关于XX的意见", "意见"),
            ("XX市公安局通告", "通告"),
            ("国务院公告", "公告"),
            ("中共中央公报", "公报"),
            ("关于XX的议案", "议案"),
        ],
    )
    def test_standard_title(self, title: str, expected: str) -> None:
        assert detect_wenzhong(title) == expected

    def test_title_with_spaces(self) -> None:
        assert detect_wenzhong("  国务院关于XX的通知  ") == "通知"

    def test_title_with_fullwidth_space(self) -> None:
        assert detect_wenzhong("国务院\u3000关于XX的通知") == "通知"


class TestDetectWenzhongByBody:
    """正文关键词匹配测试（标题不含文种词时）。"""

    @pytest.mark.parametrize(
        "title, body_text, expected",
        [
            ("关于XX工作", "现通知如下，请贯彻执行。", "通知"),
            ("关于XX事项", "现函告如下，请予函复。", "函"),
            ("关于XX问题", "现请示如下，妥否请批示。", "请示"),
            ("关于XX情况", "现报告如下，专此报告。", "报告"),
            ("关于XX请示", "现批复如下，同意。", "批复"),
            ("关于XX事件", "现通报如下。", "通报"),
        ],
    )
    def test_body_keyword_match(
        self, title: str, body_text: str, expected: str
    ) -> None:
        assert detect_wenzhong(title, body_text) == expected


class TestDetectWenzhongFallback:
    """无法识别时返回 None。"""

    def test_empty_title_no_body(self) -> None:
        assert detect_wenzhong("") is None

    def test_unrelated_title_no_body(self) -> None:
        assert detect_wenzhong("关于XX工作") is None

    def test_unrelated_title_unrelated_body(self) -> None:
        assert detect_wenzhong("关于XX工作", "这是一段普通文本。") is None


class TestDetectWenzhongFromDoc:
    """从 Document 模型判定文种。"""

    def test_doc_with_title(self) -> None:
        from docmodels import (
            Body,
            Document,
            Font,
            MetaData,
            Page,
            PageContent,
            Paragraph,
        )

        title_para = Paragraph(
            elements=[MetaData(font=Font(text="国务院关于XX的通知"))]
        )
        body_para = Paragraph(elements=[MetaData(font=Font(text="现通知如下。"))])
        page = Page(
            page_content=PageContent(body=Body(title=title_para, main_text=[body_para]))
        )
        doc = Document(pages=[page])
        assert detect_wenzhong_from_doc(doc) == "通知"

    def test_doc_empty_title(self) -> None:
        from docmodels import Body, Document, Page, PageContent

        page = Page(page_content=PageContent(body=Body()))
        doc = Document(pages=[page])
        assert detect_wenzhong_from_doc(doc) is None
