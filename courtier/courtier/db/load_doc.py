from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence

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
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db.tables import (
    DocumentCreate,
    DocumentTable,
    DocumentUpdate,
    ElementCreate,
    ElementTable,
    ElementUpdate,
    PageCreate,
    PageTable,
    PageUpdate,
    ParagraphCreate,
    ParagraphTable,
    ParagraphUpdate,
)

logger = logging.getLogger(__name__)


def _elem_to_line_element(elem: ElementTable) -> LineElement:
    """ElementTable 行 → docmodels.LineElement（原 MetaData，已改名并删除 exist 字段）。"""
    return LineElement(
        position=Position(x0=elem.x0, y0=elem.y0, x1=elem.x1, y1=elem.y1),
        font=Font(
            font_family=elem.font_family,
            font_size=elem.font_size,
            font_weight=elem.font_weight,
            font_style=elem.font_style,
            text=elem.text,
            line_no=elem.line_no,
        ),
    )


def _assemble_page(
    page_obj: PageTable,
    paragraphs: Sequence[ParagraphTable],
    elements: Sequence[ElementTable],
) -> Page:
    """用预取好的段落/元素行在内存中组装 Page 对象（不发起任何 DB 查询）。"""
    elements_by_para: dict[int, list[ElementTable]] = defaultdict(list)
    for elem in elements:
        elements_by_para[elem.paragraph_id].append(elem)

    def table_to_paragraph(para: ParagraphTable) -> Paragraph:
        elem_list = elements_by_para.get(para.id, [])
        line_elements = [
            _elem_to_line_element(e) for e in sorted(elem_list, key=lambda x: x.line_no)
        ]
        return Paragraph(
            # 间距/缩进字段 None 直通出库（对应列均为 nullable）
            space_before=para.space_before,
            space_after=para.space_after,
            line_spacing=para.line_spacing,
            first_indent=para.first_indent,
            left_indent=para.left_indent,
            right_indent=para.right_indent,
            elements=line_elements,
            outline_level=para.outline_level,
            # alignment 列 nullable：NULL 仅出现在该列新增前的存量行，
            # 读出时回退到模型默认值 "left"
            alignment=para.alignment or "left",
        )

    # Build O(1) lookup: (section_type, order_index) → ParagraphTable
    para_map: dict[tuple[str, int], ParagraphTable] = {}
    para_by_type: dict[str, list[ParagraphTable]] = defaultdict(list)
    for p in paragraphs:
        para_map[(p.section_type, p.order_index)] = p
        para_by_type[p.section_type].append(p)

    def get_paragraph(section_type: str, order_idx: int = 0) -> Paragraph | None:
        p = para_map.get((section_type, order_idx))
        return table_to_paragraph(p) if p else None

    header = Header(
        copy_number=get_paragraph("header_copy_number"),
        classification_duration=get_paragraph("header_classification_duration"),
        urgency_level=get_paragraph("header_urgency_level"),
        issuing_logo=get_paragraph("header_issuing_logo"),
        issuing_number=get_paragraph("header_issuing_number"),
        signatory=get_paragraph("header_signatory"),
        ruling_line_pos=get_paragraph("header_ruling_line_pos"),
    )

    body_main_text_paras = sorted(
        para_by_type.get("body_main_text", []), key=lambda p: p.order_index
    )
    main_text = [table_to_paragraph(p) for p in body_main_text_paras]

    body = Body(
        title=get_paragraph("body_title"),
        addressee=get_paragraph("body_addressee"),
        main_text=main_text,
        attachment_note=get_paragraph("body_attachment_note"),
        issuing_signature=get_paragraph("body_issuing_signature"),
        issue_date=get_paragraph("body_issue_date"),
        stamp=get_paragraph("body_stamp"),
        note=get_paragraph("body_note"),
        attachments=get_paragraph("body_attachments"),
    )

    footer = Footer(
        closing_line=get_paragraph("footer_closing_line"),
        carbon_copy=get_paragraph("footer_carbon_copy"),
        issuing_office=get_paragraph("footer_issuing_office"),
        distribution_date=get_paragraph("footer_distribution_date"),
        page_number=get_paragraph("footer_page_number"),
    )

    margin = Margin(
        top_margin=page_obj.top_margin,
        bottom_margin=page_obj.bottom_margin,
        left_margin=page_obj.left_margin,
        right_margin=page_obj.right_margin,
    )

    page_content = PageContent(
        header=header,
        body=body,
        footer=footer,
        margin=margin,
    )

    # 模型重构后 Page 只有 page_content/page_no 两个字段
    return Page(page_content=page_content, page_no=page_obj.page_no)


async def load_page(session: AsyncSession, page_id: int) -> Page | None:
    """
    从数据库加载指定 page_id 的完整 Page 对象。

    Args:
        session: 已有的数据库 session，由调用方管理生命周期。
        page_id: 页面 ID。
    """
    page_repo: CRUDRepository[PageTable, PageCreate, PageUpdate] = CRUDRepository(PageTable)
    page_obj = await page_repo.get(session, page_id)
    if not page_obj:
        return None

    paragraph_repo: CRUDRepository[ParagraphTable, ParagraphCreate, ParagraphUpdate] = (
        CRUDRepository(ParagraphTable)
    )
    paragraphs = await paragraph_repo.list(
        session, page_id=page_id, order_by="section_type, order_index"
    )

    element_repo: CRUDRepository[ElementTable, ElementCreate, ElementUpdate] = CRUDRepository(
        ElementTable
    )
    para_ids = [p.id for p in paragraphs]
    if para_ids:
        elements = await element_repo.list(
            session, paragraph_id__in=para_ids, order_by="paragraph_id, line_no"
        )
    else:
        elements = []

    return _assemble_page(page_obj, paragraphs, elements)


async def load_doc(doc_id: str, db: AsyncDatabase) -> Document | None:
    """加载文档。

    按 doc_id 查询（模型重构后 Document 不再有 user_id 字段，查询键从
    user_id + doc_id 简化为 doc_id）。pages/paragraphs/elements 各用一次
    批量查询取出后在内存中组装，避免逐页逐段的 N+1 查询。

    Args:
        doc_id: 文档 ID（文件内容的 sha256 值）。
        db: AsyncDatabase 实例（必需，由调用方管理生命周期）。

    Returns:
        Document 对象；查无此 doc_id 时返回 None。
    """
    async with db.session() as session:
        doc_repo: CRUDRepository[DocumentTable, DocumentCreate, DocumentUpdate] = CRUDRepository(
            DocumentTable
        )
        page_repo: CRUDRepository[PageTable, PageCreate, PageUpdate] = CRUDRepository(PageTable)

        doc_tables = await doc_repo.list(session, doc_id=doc_id)
        if not doc_tables:
            return None
        if len(doc_tables) > 1:
            # doc_id 应为唯一键（save_doc 按 doc_id 去重）；出现重复说明
            # 存在历史脏数据，取首条并告警以便排查。
            logger.warning(
                "load_doc: duplicate rows for doc_id=%s (%d rows), using the first (id=%d)",
                doc_id,
                len(doc_tables),
                doc_tables[0].id,
            )
        doc_table = doc_tables[0]

        # 页序必须显式指定，DB 不保证返回顺序
        page_tables = await page_repo.list(session, document_id=doc_table.id, order_by="page_no")

        paragraphs: list[ParagraphTable] = []
        elements: list[ElementTable] = []
        if page_tables:
            paragraph_repo: CRUDRepository[ParagraphTable, ParagraphCreate, ParagraphUpdate] = (
                CRUDRepository(ParagraphTable)
            )
            element_repo: CRUDRepository[ElementTable, ElementCreate, ElementUpdate] = (
                CRUDRepository(ElementTable)
            )
            page_ids = [p.id for p in page_tables]
            # 整文档批量查询须显式放大 limit（CRUDRepository.list 默认封顶
            # 1000）。超出 _MAX_LIMIT 的极端文档会被静默截断——命中上限时
            # 告警提示需要分页加载（当前持久层尚无调用方，暂不实现）。
            paragraphs = await paragraph_repo.list(
                session,
                page_id__in=page_ids,
                order_by="page_id, section_type, order_index",
                limit=CRUDRepository._MAX_LIMIT,
            )
            if len(paragraphs) >= CRUDRepository._MAX_LIMIT:
                logger.warning(
                    "load_doc: doc_id=%s paragraphs hit _MAX_LIMIT (%d/%d), "
                    "result may be truncated",
                    doc_id,
                    len(paragraphs),
                    CRUDRepository._MAX_LIMIT,
                )
            para_ids = [p.id for p in paragraphs]
            if para_ids:
                elements = await element_repo.list(
                    session,
                    paragraph_id__in=para_ids,
                    order_by="paragraph_id, line_no",
                    limit=CRUDRepository._MAX_LIMIT,
                )
                if len(elements) >= CRUDRepository._MAX_LIMIT:
                    logger.warning(
                        "load_doc: doc_id=%s elements hit _MAX_LIMIT (%d/%d), "
                        "result may be truncated",
                        doc_id,
                        len(elements),
                        CRUDRepository._MAX_LIMIT,
                    )

        paras_by_page: dict[int, list[ParagraphTable]] = defaultdict(list)
        for p in paragraphs:
            paras_by_page[p.page_id].append(p)

        page_of_para = {p.id: p.page_id for p in paragraphs}
        elems_by_page: dict[int, list[ElementTable]] = defaultdict(list)
        for e in elements:
            elems_by_page[page_of_para[e.paragraph_id]].append(e)

        pages = [
            _assemble_page(
                page_obj,
                paras_by_page.get(page_obj.id, []),
                elems_by_page.get(page_obj.id, []),
            )
            for page_obj in page_tables
        ]

        return Document(
            schema_version=doc_table.schema_version,
            doc_id=doc_table.doc_id,
            total_page_num=doc_table.total_page_num,
            save_path=doc_table.save_path,
            pages=pages,
            # warnings 列 nullable：存量行为 NULL，归一化为模型默认空列表
            warnings=doc_table.warnings or [],
        )
