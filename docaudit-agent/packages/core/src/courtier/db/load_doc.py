from __future__ import annotations

import logging

from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db import AsyncDatabase, CRUDRepository
from courtier.db.tables import (
    DocumentTable, PageTable, ParagraphTable, ElementTable,
)
from docmodels import (
    Document, Page, PageContent, Header, Body, Footer, Margin,
    Position, Font, MetaData, Paragraph,
)

logger = logging.getLogger(__name__)

async def load_page(session: AsyncSession, page_id: int) -> Page | None:
    """
    从数据库加载指定 page_id 的完整 Page 对象。

    Args:
        session: 已有的数据库 session，由调用方管理生命周期。
        page_id: 页面 ID。
    """
    page_repo = CRUDRepository(PageTable)
    page_obj = await page_repo.get(session, page_id)
    if not page_obj:
        return None

    paragraph_repo = CRUDRepository(ParagraphTable)
    paragraphs = await paragraph_repo.list(
        session,
        page_id=page_id,
        order_by="section_type, order_index"
    )

    element_repo = CRUDRepository(ElementTable)
    para_ids = [p.id for p in paragraphs]
    if para_ids:
        elements = await element_repo.list(
            session,
            paragraph_id__in=para_ids,
            order_by="paragraph_id, line_no"
        )
    else:
        elements = []

    elements_by_para = defaultdict(list)
    for elem in elements:
        elements_by_para[elem.paragraph_id].append(elem)

    def elem_to_metadata(elem: ElementTable) -> MetaData:
        return MetaData(
            exist=elem.exist,
            position=Position(x0=elem.x0, y0=elem.y0, x1=elem.x1, y1=elem.y1),
            font=Font(
                font_family=elem.font_family,
                font_size=elem.font_size,
                font_weight=elem.font_weight,
                font_style=elem.font_style,
                text=elem.text,
                line_no=elem.line_no
            )
        )

    def table_to_paragraph(para: ParagraphTable) -> Paragraph:
        elem_list = elements_by_para.get(para.id, [])
        metadata_list = [elem_to_metadata(e) for e in sorted(elem_list, key=lambda x: x.line_no)]
        return Paragraph(
            space_before=para.space_before,
            space_after=para.space_after,
            line_spacing=para.line_spacing,
            first_indent=para.first_indent,
            left_indent=para.left_indent,
            right_indent=para.right_indent,
            block_no=para.block_no,
            elements=metadata_list,
            outline_level=para.outline_level
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
        para_by_type.get("body_main_text", []),
        key=lambda p: p.order_index
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

    page = Page(
        raw=page_obj.raw or b'',
        page_content=page_content,
        save_path=page_obj.save_path,
        page_no=page_obj.page_no,
    )
    return page

async def load_doc(user_id: str, doc_id: str, db: AsyncDatabase):
    """加载文档。

    Args:
        user_id: 用户 ID。
        doc_id: 文档 ID。
        db: AsyncDatabase 实例（必需，由调用方管理生命周期）。

    Returns:
        Document 对象，如果未找到则返回空 Document。
    """
    pages = []
    async with db.session() as session:
        doc_repo = CRUDRepository(DocumentTable)
        page_repo = CRUDRepository(PageTable)
        doc_tables = await doc_repo.list(session, user_id=user_id, doc_id=doc_id)
        if len(doc_tables) != 0:
            doc_table = doc_tables[0]
            page_tables = await page_repo.list(session, document_id=doc_table.id)
            for page in page_tables:
                page_obj = await load_page(session, page.id)
                pages.append(page_obj)

            return Document(
                user_id=doc_table.user_id, doc_id=doc_table.doc_id,
                total_page_num=len(pages), save_path=doc_table.save_path,
                pages=pages
            )
    return Document(user_id="", doc_id="", total_page_num=0, save_path="", pages=[])