from __future__ import annotations

import logging
from typing import Any

from docmodels import Document, Page, Paragraph
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db._utils import _DEFAULT_DB_URL, _ensure_db_url, iter_section_paragraphs
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
    ResourceTable,
)

logger = logging.getLogger(__name__)


async def save_page(session: AsyncSession, page: Page, doc_id: int) -> None:
    """将 Page 对象保存到数据库。

    Args:
        session: 已有的数据库 session，由调用方管理生命周期。
        page: 要保存的 Page 对象
        doc_id: 所属文档 ID
    """
    page_repo: CRUDRepository[PageTable, PageCreate, PageUpdate] = CRUDRepository(PageTable)
    paragraph_repo: CRUDRepository[ParagraphTable, ParagraphCreate, ParagraphUpdate] = (
        CRUDRepository(ParagraphTable)
    )
    element_repo: CRUDRepository[ElementTable, ElementCreate, ElementUpdate] = CRUDRepository(
        ElementTable
    )

    # 模型重构后 Page 不再有 save_path/raw 字段；pages.save_path 列本轮已删除，
    # raw 列上一轮已删除。
    margin = page.page_content.margin
    page_data = {
        "document_id": doc_id,
        "page_no": page.page_no,
        "top_margin": margin.top_margin,
        "bottom_margin": margin.bottom_margin,
        "left_margin": margin.left_margin,
        "right_margin": margin.right_margin,
    }
    page_table = await page_repo.create(session, page_data)
    page_id = page_table.id

    paragraphs_data: list[dict[str, Any]] = []

    # Maps docmodels.Paragraph → dict for DB insertion.  A Pydantic
    # ParagraphCreate schema exists but cannot be used directly because:
    # (a) Paragraph has extra fields (elements) not in the DB model, and
    # (b) ParagraphCreate requires page_id/section_type/create_time that
    #     Paragraph does not provide.
    # 间距/缩进字段 None 直通入库（对应列均为 nullable）。
    def paragraph_to_dict(p: Paragraph, section_type: str, order_idx: int = 0) -> dict[str, Any]:
        return {
            "page_id": page_id,
            "section_type": section_type,
            "order_index": order_idx,
            "space_before": p.space_before,
            "space_after": p.space_after,
            "line_spacing": p.line_spacing,
            "first_indent": p.first_indent,
            "left_indent": p.left_indent,
            "right_indent": p.right_indent,
            "alignment": p.alignment,
            "outline_level": p.outline_level,
        }

    for para, section_type, order_idx in iter_section_paragraphs(page.page_content):
        paragraphs_data.append(paragraph_to_dict(para, section_type, order_idx))

    para_map: dict[tuple[str, int], int] = {}
    if paragraphs_data:
        inserted_paragraphs = await paragraph_repo.bulk_create(
            session, paragraphs_data, returning=True
        )
        assert isinstance(inserted_paragraphs, list)
        for p in inserted_paragraphs:
            key = (p.section_type, p.order_index)
            para_map[key] = p.id

    elements_data: list[dict[str, Any]] = []

    def collect_elements(paragraph: Paragraph, section_type: str, order_idx: int = 0):
        key = (section_type, order_idx)
        para_id = para_map.get(key)
        if not para_id:
            logger.warning("paragraph %s not found", key)
            return
        for meta in paragraph.elements:
            elements_data.append(
                {
                    "paragraph_id": para_id,
                    "x0": meta.position.x0,
                    "y0": meta.position.y0,
                    "x1": meta.position.x1,
                    "y1": meta.position.y1,
                    "font_family": meta.font.font_family,
                    "font_size": meta.font.font_size,
                    "font_weight": meta.font.font_weight,
                    "font_style": meta.font.font_style,
                    "text": meta.font.text,
                    "line_no": meta.font.line_no,
                }
            )

    for para, section_type, order_idx in iter_section_paragraphs(page.page_content):
        collect_elements(para, section_type, order_idx)

    if elements_data:
        await element_repo.bulk_create(session, elements_data)

    logger.info(
        "Page %d saved with %d paragraphs and %d elements",
        page_id,
        len(paragraphs_data),
        len(elements_data),
    )


async def save_doc(doc: Document, db: AsyncDatabase | None = None, db_url: str = ""):
    """保存文档。

    如果同一 doc_id 已有记录，先删除旧记录（级联删除
    pages/paragraphs/elements）再重新插入，确保不重复。
    （模型重构后 Document 不再有 user_id 字段，去重键从
    user_id + doc_id 简化为 doc_id。）

    整个操作在同一个事务中完成，要么全部成功，要么全部回滚。

    Args:
        db: 已有的 AsyncDatabase 实例（优先使用）
        db_url: 数据库连接 URL（当 db 为 None 时使用）
    """
    if db is None:
        resolved_url = db_url or _DEFAULT_DB_URL
        _ensure_db_url(resolved_url)
        db = AsyncDatabase(resolved_url)
        _own_db = True
    else:
        _own_db = False

    try:
        async with db.session() as session:
            doc_repo: CRUDRepository[DocumentTable, DocumentCreate, DocumentUpdate] = (
                CRUDRepository(DocumentTable)
            )

            existing = await doc_repo.list(session, doc_id=doc.doc_id)
            for old in existing:
                await session.execute(
                    delete(ResourceTable).where(ResourceTable.document_id == old.id)
                )
                await doc_repo.delete(session, old.id)
                logger.info(
                    "Dedup: deleted old document id=%d (doc_id=%s)",
                    old.id,
                    doc.doc_id,
                )

            # 模型重构后 Document 无 user_id 字段；documents.user_id 列本轮已删除。
            doc_data = {
                "doc_id": doc.doc_id,
                "schema_version": doc.schema_version,
                "total_page_num": doc.total_page_num,
                "save_path": doc.save_path,
                "warnings": doc.warnings,
            }
            doc_table = await doc_repo.create(session, doc_data)

            for page in doc.pages:
                await save_page(session, page, doc_table.id)

            return doc_table.id
    finally:
        if _own_db:
            await db.engine.dispose()
