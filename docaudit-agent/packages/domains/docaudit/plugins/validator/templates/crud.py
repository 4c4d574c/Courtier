"""模板 CRUD 操作。

从数据库加载新格式模板（v1.0 hierarchical 为唯一规范 schema）。
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db import CRUDRepository
from courtier.db.tables import FormatTemplateTable


async def load_template_from_db(
    session: AsyncSession,
    doc_type: str,
    template_id: int | None = None,
) -> dict | None:
    """从数据库加载模板。

    Args:
        session: 数据库会话
        doc_type: 公文类型
        template_id: 指定模板ID，为 None 时加载该文种的默认模板

    Returns:
        新格式模板字典，找不到则返回 None
    """
    repo = CRUDRepository(FormatTemplateTable)

    if template_id is not None:
        row = await repo.get(session, template_id)
        if row:
            return row.content
        return None

    # 查找默认模板
    rows = await repo.list(session, doc_type=doc_type, is_default=True, limit=1)
    if rows:
        return rows[0].content
    return None
