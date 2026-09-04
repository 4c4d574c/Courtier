"""Memory service — layered, DB-backed memory with a permission matrix.

Single enforcement point for every memory read/write (UI routes and the
agent-facing ``memory_store`` host service both land here).  Layers:

- ``global``: shared team/domain knowledge — readable by any authenticated
  user, writable by admins only;
- ``user``: per-user preferences/context — readable and writable by the
  owner only; admins have no read access to other users' layers.

Every mutation appends a ``memory_changes`` audit row (content as sha256
only).  Caller identity is expressed by :class:`Identity`; the API routes
build it from the JWT payload, the plugin host service from the
dispatch-boundary injected caller parameters.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.tables.memory import (
    DOMAIN_COMMON,
    LAYER_GLOBAL,
    LAYER_USER,
    OWNER_GLOBAL,
    MemoryChangeTable,
    MemoryTable,
)

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 190
_MAX_CONTENT_CHARS = 50_000
_MAX_LIST_LIMIT = 200

#: safety cap for one injection segment (rows); the char-level caps live
#: in MemoryManager (memory_auto_inject_*).
_MAX_INJECTION_ROWS = 100


class MemoryAccessError(Exception):
    """Caller may not perform this operation on the target layer."""


class MemoryValidationError(Exception):
    """Entry fields failed validation (title/domain/content)."""


class MemoryNotFoundError(Exception):
    """No entry with this id in the accessible scope."""


#: catch-all for callers that map service rejections to their own surface
#: (HTTP status codes / JSON-RPC structured errors).
MEMORY_ERRORS = (MemoryAccessError, MemoryValidationError, MemoryNotFoundError)


@dataclass(frozen=True)
class Identity:
    """Who is acting.  ``actor`` lands in the audit trail — API routes pass
    the username, the agent chain passes ``agent:<username>``."""

    owner_id: int
    is_admin: bool
    actor: str


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_fields(domain: str, title: str, content: str, known_domains: set[str] | None) -> str:
    domain = (domain or DOMAIN_COMMON).strip() or DOMAIN_COMMON
    if len(domain) > 64:
        raise MemoryValidationError("domain 过长（最多 64 字符）")
    if domain != DOMAIN_COMMON and known_domains is not None and domain not in known_domains:
        raise MemoryValidationError(f"未知领域包：{domain}")
    title = (title or "").strip()
    if not title:
        raise MemoryValidationError("title 不能为空")
    if len(title) > _MAX_TITLE_CHARS:
        raise MemoryValidationError(f"title 过长（最多 {_MAX_TITLE_CHARS} 字符）")
    content = content or ""
    if not content.strip():
        raise MemoryValidationError("content 不能为空")
    if len(content) > _MAX_CONTENT_CHARS:
        raise MemoryValidationError(f"content 过长（最多 {_MAX_CONTENT_CHARS} 字符）")
    return domain


def _entry_dict(row: MemoryTable) -> dict:
    return {
        "id": row.id,
        "layer": row.layer,
        "ownerId": row.owner_id,
        "domain": row.domain,
        "title": row.title,
        "content": row.content,
        "createdBy": row.created_by,
        "updatedBy": row.updated_by,
        "createdAt": row.created_at.isoformat() if isinstance(row.created_at, datetime) else row.created_at,
        "updatedAt": row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else row.updated_at,
    }


def _change_dict(row: MemoryChangeTable) -> dict:
    return {
        "id": row.id,
        "entryId": row.entry_id,
        "action": row.action,
        "layer": row.layer,
        "ownerId": row.owner_id,
        "domain": row.domain,
        "title": row.title,
        "oldHash": row.old_hash,
        "newHash": row.new_hash,
        "actor": row.actor,
        "createdAt": row.created_at.isoformat() if isinstance(row.created_at, datetime) else row.created_at,
    }


async def _audit(
    session: AsyncSession,
    *,
    entry_id: int,
    action: str,
    row: MemoryTable,
    old_hash: str | None,
    new_hash: str | None,
    actor: str,
) -> None:
    session.add(
        MemoryChangeTable(
            entry_id=entry_id,
            action=action,
            layer=row.layer,
            owner_id=row.owner_id,
            domain=row.domain,
            title=row.title,
            old_hash=old_hash,
            new_hash=new_hash,
            actor=actor,
        )
    )


async def _find_by_address(
    session: AsyncSession, *, layer: str, owner_id: int, domain: str, title: str
) -> MemoryTable | None:
    rows = await session.execute(
        select(MemoryTable).where(
            MemoryTable.layer == layer,
            MemoryTable.owner_id == owner_id,
            MemoryTable.domain == domain,
            MemoryTable.title == title,
        )
    )
    return rows.scalar_one_or_none()


async def _get_accessible(
    session: AsyncSession, entry_id: int, identity: Identity
) -> MemoryTable:
    """Fetch an entry the identity may see, else raise."""
    row = (await session.execute(select(MemoryTable).where(MemoryTable.id == entry_id))).scalar_one_or_none()
    if row is None:
        raise MemoryNotFoundError(f"记忆条目不存在：{entry_id}")
    if row.layer == LAYER_USER and row.owner_id != identity.owner_id:
        # 用户层互相不可见——admin 也不行（对齐结论 7）。
        raise MemoryNotFoundError(f"记忆条目不存在：{entry_id}")
    return row


def _require_layer_write(layer: str, identity: Identity) -> None:
    if layer == LAYER_GLOBAL and not identity.is_admin:
        raise MemoryAccessError("仅管理员可写全局共享层")
    # user 层写：入口已把 owner 固定为 identity.owner_id，无越权路径。


# --------------------------------------------------------------------- queries


def _layer_filters(layer: str, owner_id: int, domain: str | None, query: str):
    conds = [MemoryTable.layer == layer]
    conds.append(MemoryTable.owner_id == (OWNER_GLOBAL if layer == LAYER_GLOBAL else owner_id))
    if domain and domain != "all":
        conds.append(MemoryTable.domain == domain)
    if query:
        like = f"%{query}%"
        conds.append(or_(MemoryTable.title.like(like), MemoryTable.content.like(like)))
    return conds


async def list_entries(
    session: AsyncSession,
    *,
    layer: str,
    identity: Identity,
    domain: str | None = None,
    query: str = "",
    skip: int = 0,
    limit: int = 100,
) -> list[dict]:
    """List entries of *layer* for the caller's accessible scope.

    global → anyone authenticated; user → 仅本人层。"""
    if layer not in (LAYER_GLOBAL, LAYER_USER):
        raise MemoryValidationError(f"未知层：{layer}")
    rows = await session.execute(
        select(MemoryTable)
        .where(*_layer_filters(layer, identity.owner_id, domain, query))
        .order_by(MemoryTable.domain, MemoryTable.updated_at.desc())
        .offset(max(0, skip))
        .limit(max(1, min(limit, _MAX_LIST_LIMIT)))
    )
    return [_entry_dict(r) for r in rows.scalars().all()]


async def get_entry(session: AsyncSession, entry_id: int, *, identity: Identity) -> dict:
    row = await _get_accessible(session, entry_id, identity)
    return _entry_dict(row)


# -------------------------------------------------------------------- mutations


async def upsert_entry(
    session: AsyncSession,
    *,
    layer: str,
    title: str,
    content: str,
    domain: str = DOMAIN_COMMON,
    identity: Identity,
    known_domains: set[str] | None = None,
) -> dict:
    """Create or update (by layer+owner+domain+title) with audit."""
    _require_layer_write(layer, identity)
    domain = _validate_fields(domain, title, content, known_domains)
    title = title.strip()
    owner = OWNER_GLOBAL if layer == LAYER_GLOBAL else identity.owner_id

    row = await _find_by_address(session, layer=layer, owner_id=owner, domain=domain, title=title)
    if row is None:
        row = MemoryTable(
            layer=layer,
            owner_id=owner,
            domain=domain,
            title=title,
            content=content,
            created_by=identity.actor,
            updated_by=identity.actor,
        )
        session.add(row)
        await session.flush()
        await _audit(
            session,
            entry_id=row.id,
            action="create",
            row=row,
            old_hash=None,
            new_hash=_content_hash(content),
            actor=identity.actor,
        )
    else:
        old_hash = _content_hash(row.content)
        if old_hash == _content_hash(content) and row.domain == domain:
            return _entry_dict(row)  # 无变化不刷审计
        row.content = content
        row.updated_by = identity.actor
        await _audit(
            session,
            entry_id=row.id,
            action="update",
            row=row,
            old_hash=old_hash,
            new_hash=_content_hash(content),
            actor=identity.actor,
        )
    await session.commit()
    return _entry_dict(row)


async def update_entry(
    session: AsyncSession,
    entry_id: int,
    *,
    identity: Identity,
    title: str | None = None,
    content: str | None = None,
    domain: str | None = None,
    known_domains: set[str] | None = None,
) -> dict:
    """Patch content/title/domain of an accessible entry (audit on change)."""
    row = await _get_accessible(session, entry_id, identity)
    _require_layer_write(row.layer, identity)
    new_title = (title if title is not None else row.title).strip()
    new_domain = domain if domain is not None else row.domain
    new_content = content if content is not None else row.content
    new_domain = _validate_fields(new_domain, new_title, new_content, known_domains)

    old_hash = _content_hash(row.content)
    if (
        new_content == row.content
        and new_title == row.title
        and new_domain == row.domain
    ):
        return _entry_dict(row)

    # 改地址（title/domain）撞上已有条目 → 拒绝，让调用方自行合并。
    if (new_title, new_domain) != (row.title, row.domain):
        clash = await _find_by_address(
            session,
            layer=row.layer,
            owner_id=row.owner_id,
            domain=new_domain,
            title=new_title,
        )
        if clash is not None and clash.id != row.id:
            raise MemoryValidationError("目标地址已存在同名条目，请先合并或删除")

    row.title = new_title
    row.domain = new_domain
    row.content = new_content
    row.updated_by = identity.actor
    await _audit(
        session,
        entry_id=row.id,
        action="update",
        row=row,
        old_hash=old_hash,
        new_hash=_content_hash(new_content),
        actor=identity.actor,
    )
    await session.commit()
    return _entry_dict(row)


async def delete_entry(session: AsyncSession, entry_id: int, *, identity: Identity) -> None:
    row = await _get_accessible(session, entry_id, identity)
    _require_layer_write(row.layer, identity)
    await _audit(
        session,
        entry_id=row.id,
        action="delete",
        row=row,
        old_hash=_content_hash(row.content),
        new_hash=None,
        actor=identity.actor,
    )
    await session.delete(row)
    await session.commit()


async def clear_layer(
    session: AsyncSession, *, layer: str, identity: Identity
) -> int:
    """Delete every entry in the caller's scope of *layer* (one audit row each)."""
    if layer == LAYER_GLOBAL:
        _require_layer_write(LAYER_GLOBAL, identity)
    rows = (
        (await session.execute(select(MemoryTable).where(*_layer_filters(layer, identity.owner_id, None, ""))))
        .scalars()
        .all()
    )
    for row in rows:
        await _audit(
            session,
            entry_id=row.id,
            action="delete",
            row=row,
            old_hash=_content_hash(row.content),
            new_hash=None,
            actor=identity.actor,
        )
    count = len(rows)
    if count:
        await session.execute(
            delete(MemoryTable).where(*_layer_filters(layer, identity.owner_id, None, ""))
        )
        await session.commit()
    return count


# ------------------------------------------------------------- agent tool surface


async def _search_by_title(
    session: AsyncSession, *, identity: Identity, title: str, domain: str | None = None
) -> dict | None:
    """Find an entry by exact title in the caller's visible scope —
    user layer first, then global (own entry wins over a same-titled
    global one).  Domain filters only when explicitly given."""
    for layer, owner in ((LAYER_USER, identity.owner_id), (LAYER_GLOBAL, OWNER_GLOBAL)):
        conds = [
            MemoryTable.layer == layer,
            MemoryTable.owner_id == owner,
            MemoryTable.title == title.strip(),
        ]
        if domain:
            conds.append(MemoryTable.domain == domain)
        row = (
            await session.execute(select(MemoryTable).where(*conds).limit(1))
        ).scalar_one_or_none()
        if row is not None:
            return _entry_dict(row)
    return None


async def tool_action(
    session: AsyncSession,
    *,
    identity: Identity,
    action: str,
    entry_id: int | None = None,
    title: str | None = None,
    domain: str | None = None,
    content: str | None = None,
    layer: str | None = None,
    known_domains: set[str] | None = None,
) -> dict:
    """The agent-facing ``memory`` tool surface (list/read/write/delete).

    Same enforcement and audit as the UI routes — this is only an
    argument-shape adapter, never a second permission path.
    """
    action = (action or "").strip().lower()

    if action == "list":
        return {
            "user": await list_entries(
                session, layer=LAYER_USER, identity=identity, domain=domain
            ),
            "global": await list_entries(
                session, layer=LAYER_GLOBAL, identity=identity, domain=domain
            ),
        }

    if action == "read":
        if entry_id is not None:
            return await get_entry(session, int(entry_id), identity=identity)
        if not (title or "").strip():
            raise MemoryValidationError("read 需要 entry_id 或 title")
        row = await _search_by_title(session, identity=identity, title=title, domain=domain)
        if row is None:
            raise MemoryNotFoundError(f"找不到记忆条目：{title}")
        return row

    if action == "write":
        eff_layer = (layer or LAYER_USER).strip().lower()
        if eff_layer not in (LAYER_USER, LAYER_GLOBAL):
            raise MemoryValidationError(f"未知层：{layer}")
        return await upsert_entry(
            session,
            layer=eff_layer,
            title=title or "",
            content=content or "",
            domain=domain or DOMAIN_COMMON,
            identity=identity,
            known_domains=known_domains,
        )

    if action == "delete":
        if entry_id is not None:
            await delete_entry(session, int(entry_id), identity=identity)
            return {"deleted": int(entry_id)}
        if not (title or "").strip():
            raise MemoryValidationError("delete 需要 entry_id 或 title")
        row = await _search_by_title(session, identity=identity, title=title, domain=domain)
        if row is None:
            raise MemoryNotFoundError(f"找不到记忆条目：{title}")
        await delete_entry(session, int(row["id"]), identity=identity)
        return {"deleted": int(row["id"])}

    raise MemoryValidationError(f"未知 action：{action}（可用：list/read/write/delete）")


# ------------------------------------------------------- audit + injection + cascade

async def list_changes(
    session: AsyncSession,
    *,
    entry_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Audit trail (admin-only route enforces the role)."""
    conds = []
    if entry_id is not None:
        conds.append(MemoryChangeTable.entry_id == entry_id)
    stmt = (
        select(MemoryChangeTable)
        .where(*conds)
        .order_by(MemoryChangeTable.id.desc())
        .offset(max(0, skip))
        .limit(max(1, min(limit, _MAX_LIST_LIMIT)))
    )
    rows = await session.execute(stmt)
    return [_change_dict(r) for r in rows.scalars().all()]


async def collect_for_injection(
    session: AsyncSession,
    *,
    owner_id: int,
    domains: list[str],
) -> dict[str, list[dict]]:
    """Entries for the recall injection: user layer first, then global.

    Each side contributes its ``common`` entries plus entries of *domains*
    (the session's active domain set).  Returns ``{"user": [...], "global":
    [...]}`` with rows ordered common-first, then by recency.
    """
    async def _segment(layer: str) -> list[dict]:
        conds = [MemoryTable.layer == layer]
        conds.append(
            MemoryTable.owner_id == (OWNER_GLOBAL if layer == LAYER_GLOBAL else owner_id)
        )
        conds.append(
            MemoryTable.domain.in_([DOMAIN_COMMON, *domains]) if domains else MemoryTable.domain == DOMAIN_COMMON
        )
        rows = await session.execute(
            select(MemoryTable)
            .where(*conds)
            .order_by(MemoryTable.domain, MemoryTable.updated_at.desc())
            .limit(_MAX_INJECTION_ROWS)
        )
        return [_entry_dict(r) for r in rows.scalars().all()]

    return {"user": await _segment(LAYER_USER), "global": await _segment(LAYER_GLOBAL)}


async def delete_user_memory(session: AsyncSession, owner_id: int) -> int:
    """Remove every user-layer entry of *owner_id* (account deletion cascade).

    Audit rows keep the deletion record with actor ``system`` — content
    stays unrecoverable by design.
    """
    rows = (
        (await session.execute(select(MemoryTable).where(MemoryTable.layer == LAYER_USER, MemoryTable.owner_id == owner_id)))
        .scalars()
        .all()
    )
    for row in rows:
        await _audit(
            session,
            entry_id=row.id,
            action="delete",
            row=row,
            old_hash=_content_hash(row.content),
            new_hash=None,
            actor="system",
        )
    count = len(rows)
    if count:
        await session.execute(
            delete(MemoryTable).where(MemoryTable.layer == LAYER_USER, MemoryTable.owner_id == owner_id)
        )
        await session.commit()
    return count
