"""Resource library service — ingest documents into the ES chunks index.

Pipeline: save upload → MD5 → MinIO → extract text → split chunks →
MySQL metadata row → ES bulk index.  Deletion removes all three layers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, func, or_, select

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db.tables.resource import ResourceCreate, ResourceTable, ResourceUpdate
from courtier.es import bulk_index_chunks, delete_by_resource_id
from courtier.storage import put_object, remove_object

logger = logging.getLogger(__name__)

resource_repo: CRUDRepository[ResourceTable, ResourceCreate, ResourceUpdate] = CRUDRepository(
    ResourceTable
)

ALLOWED_EXTS = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_SIZE = 50 * 1024 * 1024
_CHUNK_SIZE = 1000
_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


# -- Text extraction ------------------------------------------------------------


def _extract_text(file_path: Path) -> str:
    """Extract plain text from PDF / DOCX / plain-text files."""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        import fitz

        parts: list[str] = []
        with fitz.open(file_path) as doc:
            for page in doc:
                parts.append(page.get_text("text"))
        return "\n".join(parts)
    if ext == ".docx":
        import docx

        doc = docx.Document(str(file_path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    return file_path.read_text(encoding="utf-8", errors="ignore")


def _split_chunks(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """Split text into ~chunk_size chunks, preserving paragraph boundaries.

    Paragraphs are accumulated until adding the next one would exceed the
    size limit; a single paragraph longer than the limit is hard-split.
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:chunk_size])
            para = para[chunk_size:]
        if not para:
            continue
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) > chunk_size and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# -- Ingest / list / delete -----------------------------------------------------


async def ingest_resource(
    file: UploadFile,
    *,
    title: str,
    author: str,
    source: str,
    tags: str,
    publish_date: date | None,
    owner_name: str,
    owner_id: int | None,
    is_admin: bool,
    visibility: str = "public",
    settings: Any,
    db: AsyncDatabase,
) -> ResourceTable:
    """Ingest an uploaded document into the resource library (ES + MySQL + MinIO).

    Visibility rules: non-admins always upload to their personal library;
    admins choose (default public).  Chunks carry visibility/owner_id so
    search-time filtering can enforce the same boundary.
    """
    if not settings.es_hosts:
        raise HTTPException(503, "Elasticsearch 未配置，无法使用资源库")
    if visibility not in ("personal", "public"):
        raise HTTPException(400, "visibility 必须是 personal 或 public")
    if not is_admin:
        visibility = "personal"

    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            400, f"不支持的文件类型 {ext or '(无扩展名)'}，支持：pdf / docx / txt / md"
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "文件过大，请上传小于 50 MB 的文件")
    if not content:
        raise HTTPException(400, "文件内容为空")

    md5_hex = hashlib.md5(content).hexdigest()
    object_key = f"{md5_hex}/{filename}"

    # MinIO raw-file storage is best-effort: the ES chunks are the searchable
    # payload, so a storage outage should not block ingestion.
    if settings.minio_endpoint:
        try:
            await asyncio.to_thread(
                put_object,
                settings.minio_bucket_resources,
                object_key,
                content,
                _MIME_BY_EXT.get(ext, "application/octet-stream"),
            )
        except Exception:
            logger.warning("MinIO upload failed for %s", object_key, exc_info=True)

    # Extract + chunk (blocking parsers off the event loop).
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        text = await asyncio.to_thread(_extract_text, tmp_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if not text.strip():
        raise HTTPException(400, "未能从文件中提取到文本内容")

    chunks = _split_chunks(text)
    total_chars = sum(len(c) for c in chunks)

    async with db.session() as session:
        resource = await resource_repo.create(
            session,
            ResourceCreate(
                title=title or Path(filename).stem,
                author=author or None,
                source=source or None,
                tags=tags or None,
                publish_date=publish_date,
                file_type=ext.lstrip("."),
                file_size=len(content),
                minio_path=object_key,
                md5=md5_hex,
                chunk_count=len(chunks),
                char_count=total_chars,
                status="ready",
                resource_type="manual",
                owner_id=owner_id,
                visibility=visibility,
            ),
        )
        await session.commit()

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    now = datetime.now(timezone.utc).isoformat()
    actions: list[dict] = []
    for i, chunk in enumerate(chunks):
        body: dict[str, Any] = {
            "resource_id": resource.id,
            "doc_type": ext.lstrip("."),
            "chunk_no": i,
            "chunk_text": chunk,
            "title": resource.title,
            "author": author or "",
            "user_id": owner_name,
            "visibility": visibility,
            "char_count": len(chunk),
            "audit_status": "library",
            "created_at": now,
        }
        if owner_id is not None:
            body["owner_id"] = owner_id
        if tag_list:
            body["tags"] = tag_list
        if publish_date is not None:
            body["publish_date"] = publish_date.isoformat()
        actions.append({"index": {"_index": settings.es_index_chunks, "_id": f"{resource.id}_{i}"}})
        actions.append(body)

    try:
        await asyncio.to_thread(bulk_index_chunks, actions)
    except Exception as exc:
        logger.exception("ES bulk index failed for resource %s", resource.id)
        async with db.session() as session:
            await resource_repo.delete(session, resource.id)
            await session.commit()
        raise HTTPException(502, f"Elasticsearch 索引写入失败：{exc}") from exc

    return resource


def _to_summary(r: ResourceTable) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "author": r.author,
        "source": r.source,
        "tags": r.tags,
        "publishDate": r.publish_date.isoformat() if r.publish_date else None,
        "fileType": r.file_type,
        "fileSize": r.file_size,
        "chunkCount": r.chunk_count,
        "charCount": r.char_count,
        "status": r.status,
        "visibility": r.visibility,
        "ownerId": r.owner_id,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


def _visibility_filter(owner_id: int | None, is_admin: bool, scope: str):
    """Build the WHERE clause for list visibility.

    - scope=public: only public rows (both roles).
    - scope=personal: only the caller's own personal rows (admin with
      owner_id=None would see nothing — admins pass their own id like
      everyone else).
    - scope=all (default): normal users see public + own personal; admins
      see everything (platform management).
    """
    if scope == "public":
        return ResourceTable.visibility == "public"
    if scope == "personal":
        return and_(
            ResourceTable.visibility == "personal",
            ResourceTable.owner_id == (owner_id if owner_id is not None else -1),
        )
    if is_admin:
        return None
    clauses = [ResourceTable.visibility == "public"]
    if owner_id is not None:
        clauses.append(
            and_(
                ResourceTable.visibility == "personal",
                ResourceTable.owner_id == owner_id,
            )
        )
    return or_(*clauses)


async def list_resources(
    db: AsyncDatabase,
    *,
    skip: int = 0,
    limit: int = 50,
    query: str = "",
    owner_id: int | None = None,
    is_admin: bool = False,
    scope: str = "all",
) -> dict[str, Any]:
    """List library resources, newest first, with fuzzy title/author/source/tags search.

    Visibility: normal users see public + their own personal rows; admins
    see everything.  *scope* narrows to a single library for UI tabs.
    """
    where_clauses = []
    vis = _visibility_filter(owner_id, is_admin, scope)
    if vis is not None:
        where_clauses.append(vis)
    if query.strip():
        like = f"%{query.strip()}%"
        where_clauses.append(
            or_(
                ResourceTable.title.ilike(like),
                ResourceTable.author.ilike(like),
                ResourceTable.source.ilike(like),
                ResourceTable.tags.ilike(like),
            )
        )
    async with db.session() as session:
        stmt = select(ResourceTable)
        count_stmt = select(func.count()).select_from(ResourceTable)
        for clause in where_clauses:
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        stmt = stmt.order_by(ResourceTable.created_at.desc())
        rows = (await session.execute(stmt.offset(skip).limit(limit))).scalars().all()
        total = (await session.execute(count_stmt)).scalar_one()
    return {
        "total": total,
        "items": [_to_summary(r) for r in rows],
    }


async def delete_resource(
    db: AsyncDatabase,
    settings: Any,
    resource_id: int,
    *,
    owner_id: int | None = None,
    is_admin: bool = False,
) -> ResourceTable | None:
    """Delete a resource from ES, MySQL, and MinIO. Returns the row if found.

    Permission: admins may delete anything; others only their own personal
    rows.  Raises 403 when the caller may not delete the resource.
    """
    async with db.session() as session:
        resource = await resource_repo.get(session, resource_id)
        if resource is None:
            return None
        if not is_admin:
            if resource.visibility != "personal" or resource.owner_id != owner_id:
                raise HTTPException(403, "只能删除自己个人资源库中的条目")
        await resource_repo.delete(session, resource_id)
        await session.commit()

    try:
        await asyncio.to_thread(delete_by_resource_id, resource_id)
    except Exception:
        logger.warning("ES chunk deletion failed for resource %s", resource_id, exc_info=True)
    if settings.minio_endpoint and resource.minio_path:
        try:
            await asyncio.to_thread(
                remove_object, settings.minio_bucket_resources, resource.minio_path
            )
        except Exception:
            logger.warning(
                "MinIO object deletion failed for %s",
                resource.minio_path,
                exc_info=True,
            )
    return resource
