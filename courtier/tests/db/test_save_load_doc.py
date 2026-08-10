"""save_doc/load_doc 与新 docmodels 契约的往返测试。

不连真实数据库：用 fake CRUDRepository 记录 save 侧写入的 dict，
并构造表行（SimpleNamespace）喂给 load 侧，验证两侧映射逻辑。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from docmodels import (
    Body,
    Document,
    Font,
    Header,
    LineElement,
    Margin,
    Page,
    PageContent,
    Paragraph,
    Position,
)

from courtier.db.db_manager import CRUDRepository
from courtier.db.load_doc import load_doc, load_page
from courtier.db.save_doc import save_doc, save_page
from courtier.db.tables import (
    DocumentTable,
    ElementTable,
    PageTable,
    ParagraphTable,
)

# ---------------------------------------------------------------------------
# 构造 docmodels 测试对象
# ---------------------------------------------------------------------------


def _line_element(text: str = "正文一行", line_no: int = 0) -> LineElement:
    return LineElement(
        position=Position(x0=1.0, y0=2.0, x1=3.0, y1=4.0),
        font=Font(font_family="仿宋", font_size=16.0, text=text, line_no=line_no),
    )


def _sample_page() -> Page:
    return Page(
        page_no=0,
        page_content=PageContent(
            header=Header(urgency_level=Paragraph(alignment="right")),
            body=Body(
                title=Paragraph(alignment="center", elements=[_line_element("标题")]),
                main_text=[
                    Paragraph(
                        space_before=None,
                        space_after=None,
                        line_spacing=28.0,
                        first_indent=None,
                        left_indent=None,
                        right_indent=None,
                        alignment="justify",
                        outline_level="body_text",
                        elements=[_line_element("第一段"), _line_element("第二行", line_no=1)],
                    ),
                ],
            ),
            margin=Margin(top_margin=37.0, bottom_margin=35.0, left_margin=28.0, right_margin=26.0),
        ),
    )


# ---------------------------------------------------------------------------
# save 侧 fake repo
# ---------------------------------------------------------------------------


def _make_save_repos():
    calls: dict[str, list] = {"page_create": [], "para_bulk": [], "elem_bulk": []}

    page_repo = MagicMock()

    async def page_create(session, data):
        calls["page_create"].append(data)
        return SimpleNamespace(id=101)

    page_repo.create = page_create

    para_repo = MagicMock()

    async def para_bulk(session, data_list, returning=False):
        calls["para_bulk"].append(list(data_list))
        return [
            SimpleNamespace(
                section_type=d["section_type"], order_index=d["order_index"], id=1000 + i
            )
            for i, d in enumerate(data_list)
        ]

    para_repo.bulk_create = para_bulk

    elem_repo = MagicMock()

    async def elem_bulk(session, data_list):
        calls["elem_bulk"].append(list(data_list))
        return len(data_list)

    elem_repo.bulk_create = elem_bulk

    repos = {PageTable: page_repo, ParagraphTable: para_repo, ElementTable: elem_repo}
    return calls, MagicMock(side_effect=lambda model: repos[model])


class TestSavePage:
    async def test_drops_removed_fields_and_keeps_alignment(self):
        calls, repo_factory = _make_save_repos()
        with patch("courtier.db.save_doc.CRUDRepository", repo_factory):
            await save_page(MagicMock(), _sample_page(), doc_id=1)

        # page 行：不再写 raw / save_path，保留页码与页边距
        (page_row,) = calls["page_create"]
        assert "raw" not in page_row
        assert "save_path" not in page_row
        assert page_row["document_id"] == 1
        assert page_row["page_no"] == 0
        assert page_row["top_margin"] == 37.0

        # paragraph 行：无 block_no；alignment 入库；None 间距直通
        para_rows = calls["para_bulk"][0]
        assert len(para_rows) == 3  # header_urgency_level + body_title + body_main_text
        for row in para_rows:
            assert "block_no" not in row
            assert "exist" not in row
        by_type = {r["section_type"]: r for r in para_rows}
        assert by_type["header_urgency_level"]["alignment"] == "right"
        assert by_type["body_title"]["alignment"] == "center"
        main = by_type["body_main_text"]
        assert main["alignment"] == "justify"
        assert main["space_before"] is None
        assert main["line_spacing"] == 28.0
        assert main["outline_level"] == "body_text"

        # element 行：无 exist；文本/行号保留
        elem_rows = calls["elem_bulk"][0]
        assert len(elem_rows) == 3
        for row in elem_rows:
            assert "exist" not in row
        texts = sorted(r["text"] for r in elem_rows)
        assert texts == ["标题", "第一段", "第二行"]

    async def test_empty_skeleton_produces_no_placeholder_rows(self):
        """空骨架（全部槽位 None、main_text 为空）不再产生占位段落/元素行。"""
        calls, repo_factory = _make_save_repos()
        with patch("courtier.db.save_doc.CRUDRepository", repo_factory):
            await save_page(MagicMock(), Page(page_no=0), doc_id=1)

        assert len(calls["page_create"]) == 1
        assert calls["para_bulk"] == []
        assert calls["elem_bulk"] == []


class TestSaveDoc:
    def _fake_db(self, session):
        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        return db

    async def test_dedup_by_doc_id_and_doc_row_fields(self):
        session = AsyncMock()
        doc_repo = MagicMock()
        doc_repo.list = AsyncMock(return_value=[SimpleNamespace(id=55)])
        doc_repo.delete = AsyncMock()
        doc_repo.create = AsyncMock(return_value=SimpleNamespace(id=7))
        repo_factory = MagicMock(side_effect=lambda model: doc_repo)

        doc = Document(
            schema_version="1.0",
            doc_id="sha256abc",
            total_page_num=1,
            save_path="/tmp/a.docx",
            pages=[_sample_page()],
            warnings=["扫描页未识别"],
        )
        with (
            patch("courtier.db.save_doc.CRUDRepository", repo_factory),
            patch("courtier.db.save_doc.save_page", new=AsyncMock()) as mock_save_page,
        ):
            new_id = await save_doc(doc, db=self._fake_db(session))

        assert new_id == 7
        # 去重键只剩 doc_id（模型已无 user_id）
        _, list_kwargs = doc_repo.list.call_args
        assert list_kwargs == {"doc_id": "sha256abc"}
        doc_repo.delete.assert_awaited_once_with(session, 55)

        # documents 行：不写 user_id；warnings/schema_version 入库
        doc_data = doc_repo.create.call_args[0][1]
        assert "user_id" not in doc_data
        assert doc_data["doc_id"] == "sha256abc"
        assert doc_data["schema_version"] == "1.0"
        assert doc_data["warnings"] == ["扫描页未识别"]
        assert doc_data["total_page_num"] == 1
        assert doc_data["save_path"] == "/tmp/a.docx"

        mock_save_page.assert_awaited_once()


# ---------------------------------------------------------------------------
# load 侧 fake 表行
# ---------------------------------------------------------------------------


def _page_row(id: int, page_no: int, document_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        document_id=document_id,
        page_no=page_no,
        save_path="默认路径",
        top_margin=37.0,
        bottom_margin=35.0,
        left_margin=28.0,
        right_margin=26.0,
    )


def _para_row(id: int, page_id: int, section_type: str, order_index: int = 0, **over):
    row = dict(
        id=id,
        page_id=page_id,
        section_type=section_type,
        order_index=order_index,
        space_before=None,
        space_after=None,
        line_spacing=None,
        first_indent=None,
        left_indent=None,
        right_indent=None,
        alignment=None,
        outline_level="others",
    )
    row.update(over)
    return SimpleNamespace(**row)


def _elem_row(id: int, paragraph_id: int, line_no: int = 0, text: str = "正文一行"):
    return SimpleNamespace(
        id=id,
        paragraph_id=paragraph_id,
        x0=1.0,
        y0=2.0,
        x1=3.0,
        y1=4.0,
        font_family="仿宋",
        font_size=16.0,
        font_weight=False,
        font_style=False,
        text=text,
        line_no=line_no,
    )


def _doc_row(**over) -> SimpleNamespace:
    row = dict(
        id=1,
        doc_id="sha256abc",
        schema_version="1.0",
        total_page_num=2,
        save_path="/tmp/a.docx",
        warnings=["扫描页未识别"],
    )
    row.update(over)
    return SimpleNamespace(**row)


def _make_load_repos(
    doc_rows=None, page_rows=None, para_rows=None, elem_rows=None
) -> tuple[dict[str, MagicMock], MagicMock]:
    doc_repo, page_repo, para_repo, elem_repo = (
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    doc_repo.list = AsyncMock(return_value=doc_rows or [])
    page_repo.list = AsyncMock(return_value=page_rows or [])
    page_repo.get = AsyncMock(return_value=(page_rows or [None])[0])
    para_repo.list = AsyncMock(return_value=para_rows or [])
    elem_repo.list = AsyncMock(return_value=elem_rows or [])
    repos = {
        DocumentTable: doc_repo,
        PageTable: page_repo,
        ParagraphTable: para_repo,
        ElementTable: elem_repo,
    }
    factory = MagicMock(side_effect=lambda model: repos[model])
    # load_doc 通过 CRUDRepository._MAX_LIMIT 读取批量查询上限；
    # patch 后类属性也会落到 mock 上，这里补回真实值
    factory._MAX_LIMIT = CRUDRepository._MAX_LIMIT
    return repos, factory


class TestLoadDoc:
    async def test_not_found_returns_none(self):
        repos, repo_factory = _make_load_repos(doc_rows=[])
        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with patch("courtier.db.load_doc.CRUDRepository", repo_factory):
            result = await load_doc("missing", db=db)

        assert result is None
        # 查文档时不再带 user_id
        _, list_kwargs = repos[DocumentTable].list.call_args
        assert list_kwargs == {"doc_id": "missing"}

    async def test_roundtrip_and_batch_queries(self):
        doc_row = _doc_row()
        # 故意乱序给出，验证页序由 order_by 约束（fake 按 DB 行为返回已排序结果）
        page_rows = [_page_row(100, page_no=0), _page_row(101, page_no=1)]
        para_rows = [
            _para_row(
                1000,
                100,
                "body_main_text",
                alignment="justify",
                line_spacing=28.0,
                outline_level="body_text",
            ),
            _para_row(1001, 101, "body_title", alignment=None),
        ]
        elem_rows = [
            _elem_row(1, 1000, line_no=0, text="第一段"),
            _elem_row(2, 1000, line_no=1, text="第二行"),
        ]
        repos, repo_factory = _make_load_repos([doc_row], page_rows, para_rows, elem_rows)

        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with patch("courtier.db.load_doc.CRUDRepository", repo_factory):
            doc = await load_doc("sha256abc", db=db)

        assert doc is not None
        # 文档级字段往返
        assert doc.schema_version == "1.0"
        assert doc.doc_id == "sha256abc"
        assert doc.total_page_num == 2
        assert doc.save_path == "/tmp/a.docx"
        assert doc.warnings == ["扫描页未识别"]
        assert not hasattr(doc, "user_id")

        # 页序：pages 查询必须带 order_by="page_no"
        _, page_kwargs = repos[PageTable].list.call_args
        assert page_kwargs["document_id"] == 1
        assert page_kwargs["order_by"] == "page_no"
        assert [p.page_no for p in doc.pages] == [0, 1]

        # N+1 消除：paragraphs/elements 各只批量查一次
        assert repos[ParagraphTable].list.await_count == 1
        _, para_kwargs = repos[ParagraphTable].list.call_args
        assert para_kwargs["page_id__in"] == [100, 101]
        assert para_kwargs["limit"] == CRUDRepository._MAX_LIMIT
        assert repos[ElementTable].list.await_count == 1
        _, elem_kwargs = repos[ElementTable].list.call_args
        assert elem_kwargs["paragraph_id__in"] == [1000, 1001]

        # 段落字段往返：alignment 读回；None 间距直通
        main = doc.pages[0].page_content.body.main_text[0]
        assert main.alignment == "justify"
        assert main.line_spacing == 28.0
        assert main.space_before is None
        assert main.outline_level == "body_text"
        # elements → LineElement（无 exist），按 line_no 排序
        assert [e.font.text for e in main.elements] == ["第一段", "第二行"]
        assert not hasattr(main.elements[0], "exist")
        # alignment 存量 NULL → 回退模型默认 "left"
        title = doc.pages[1].page_content.body.title
        assert title is not None
        assert title.alignment == "left"

    async def test_no_pages_short_circuits(self):
        repos, repo_factory = _make_load_repos(
            [_doc_row(total_page_num=0, warnings=None)], page_rows=[]
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with patch("courtier.db.load_doc.CRUDRepository", repo_factory):
            doc = await load_doc("sha256abc", db=db)

        assert doc is not None
        assert doc.pages == []
        # warnings 列 NULL → 归一化为空列表
        assert doc.warnings == []
        # 无页时不发起 paragraphs/elements 查询
        repos[ParagraphTable].list.assert_not_awaited()
        repos[ElementTable].list.assert_not_awaited()

    async def test_duplicate_doc_id_warns_and_uses_first(self, caplog):
        """同一 doc_id 多行：取首条并告警（历史脏数据可观测）。"""
        repos, repo_factory = _make_load_repos(
            doc_rows=[_doc_row(id=1), _doc_row(id=2)], page_rows=[]
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with (
            patch("courtier.db.load_doc.CRUDRepository", repo_factory),
            caplog.at_level(logging.WARNING, logger="courtier.db.load_doc"),
        ):
            doc = await load_doc("sha256abc", db=db)

        assert doc is not None
        assert any(
            "duplicate rows for doc_id=sha256abc" in r.message and "2 rows" in r.message
            for r in caplog.records
        )

    async def test_paragraphs_at_max_limit_warns(self, caplog):
        """段落批量查询命中 _MAX_LIMIT：疑似静默截断，须告警。"""
        max_limit = CRUDRepository._MAX_LIMIT
        para_rows = [_para_row(1000, 100, "body_main_text")] * max_limit
        repos, repo_factory = _make_load_repos(
            [_doc_row()], [_page_row(100, page_no=0)], para_rows=para_rows, elem_rows=[]
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with (
            patch("courtier.db.load_doc.CRUDRepository", repo_factory),
            caplog.at_level(logging.WARNING, logger="courtier.db.load_doc"),
        ):
            await load_doc("sha256abc", db=db)

        assert any(
            "paragraphs hit _MAX_LIMIT" in r.message
            and f"({max_limit}/{max_limit})" in r.message
            and "sha256abc" in r.message
            for r in caplog.records
        )

    async def test_elements_at_max_limit_warns(self, caplog):
        """元素批量查询命中 _MAX_LIMIT：疑似静默截断，须告警。"""
        max_limit = CRUDRepository._MAX_LIMIT
        elem_rows = [_elem_row(1, 1000)] * max_limit
        repos, repo_factory = _make_load_repos(
            [_doc_row()],
            [_page_row(100, page_no=0)],
            para_rows=[_para_row(1000, 100, "body_main_text")],
            elem_rows=elem_rows,
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db = MagicMock()
        db.session = session_ctx
        with (
            patch("courtier.db.load_doc.CRUDRepository", repo_factory),
            caplog.at_level(logging.WARNING, logger="courtier.db.load_doc"),
        ):
            await load_doc("sha256abc", db=db)

        assert any(
            "elements hit _MAX_LIMIT" in r.message and "sha256abc" in r.message
            for r in caplog.records
        )


class TestLoadPage:
    async def test_page_not_found_returns_none(self):
        repos, repo_factory = _make_load_repos(page_rows=[])
        with patch("courtier.db.load_doc.CRUDRepository", repo_factory):
            assert await load_page(AsyncMock(), 999) is None

    async def test_page_assembly(self):
        page_row = _page_row(100, page_no=0)
        para_rows = [
            _para_row(1000, 100, "header_urgency_level", alignment="right", space_before=12.0)
        ]
        elem_rows = [_elem_row(1, 1000, line_no=0, text="特急")]
        repos, repo_factory = _make_load_repos(
            page_rows=[page_row], para_rows=para_rows, elem_rows=elem_rows
        )
        with patch("courtier.db.load_doc.CRUDRepository", repo_factory):
            page = await load_page(AsyncMock(), 100)

        assert page is not None
        assert page.page_no == 0
        # 模型 Page 已无 raw/save_path 字段
        assert not hasattr(page, "raw")
        assert not hasattr(page, "save_path")
        urgency = page.page_content.header.urgency_level
        assert urgency is not None
        assert urgency.alignment == "right"
        assert urgency.space_before == 12.0
        assert urgency.space_after is None
        assert [e.font.text for e in urgency.elements] == ["特急"]
        # 空槽位保持 None（不产生空 Paragraph 占位）
        assert page.page_content.body.title is None
        assert page.page_content.body.main_text == []
        assert page.page_content.footer.page_number is None
