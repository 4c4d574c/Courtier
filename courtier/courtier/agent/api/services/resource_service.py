"""Resource library service — ingest documents into the ES chunks index.

Pipeline: save upload → MD5 → MinIO → extract text → split chunks →
MySQL metadata row → ES bulk index.  Deletion removes all three layers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, func, or_, select

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db.tables.resource import ResourceCreate, ResourceTable, ResourceUpdate
from courtier.es import bulk_index_chunks, delete_by_resource_id
from courtier.es.embeddings import embed_chunks
from courtier.storage import (
    get_object,
    get_presigned_url,
    object_exists,
    put_object,
    remove_object,
)

from .file_service import _read_bounded

logger = logging.getLogger(__name__)

resource_repo: CRUDRepository[ResourceTable, ResourceCreate, ResourceUpdate] = CRUDRepository(
    ResourceTable
)

ALLOWED_EXTS = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_SIZE = 50 * 1024 * 1024
_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 100
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


def _split_chunks(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = 0) -> list[str]:
    """Split text into ~chunk_size chunks, preserving paragraph boundaries.

    Paragraphs are accumulated until adding the next one would exceed the
    size limit; a single paragraph longer than the limit is hard-split.
    When *overlap* > 0, every chunk except the first is prefixed with the
    tail of the previous chunk (paragraph-aligned, up to *overlap* chars) so
    provisions spanning chunk boundaries stay retrievable as a unit.
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    raw: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > chunk_size:
            if current:
                raw.append(current)
                current = ""
            raw.append(para[:chunk_size])
            para = para[chunk_size:]
        if not para:
            continue
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) > chunk_size and current:
            raw.append(current)
            current = para
        else:
            current = candidate
    if current:
        raw.append(current)
    if overlap <= 0 or len(raw) <= 1:
        return raw
    chunks = [raw[0]]
    for prev, nxt in zip(raw, raw[1:]):
        tail = _tail_paragraphs(prev, overlap)
        chunks.append(f"{tail}\n{nxt}" if tail else nxt)
    return chunks


def _tail_paragraphs(chunk: str, max_chars: int) -> str:
    """Return the trailing up-to-*max_chars* content chars of *chunk*, aligned
    to paragraph boundaries where possible.  A single paragraph longer than
    the budget is hard-cut from its end."""
    paragraphs = [p for p in chunk.split("\n") if p]
    if not paragraphs:
        return ""
    tail: list[str] = []
    total = 0
    for para in reversed(paragraphs):
        if tail and total + len(para) > max_chars:
            break
        if total + len(para) > max_chars:
            # Only candidate and it alone exceeds the budget: hard cut.
            tail.append(para[-(max_chars - total) :])
            break
        tail.append(para)
        total += len(para)
        if total >= max_chars:
            break
    return "\n".join(reversed(tail))


# -- Ingest / list / delete -----------------------------------------------------


def build_chunk_actions(
    resource_id: int,
    chunks: list[str],
    *,
    doc_type: str,
    title: str,
    author: str,
    user_id: str,
    visibility: str,
    owner_id: int | None,
    tags: list[str],
    publish_date: date | None,
    index_name: str,
    vectors: list[list[float] | None] | None = None,
) -> list[dict]:
    """Build ES bulk actions (meta/body pairs) for one resource's chunks.

    Shared by the ingest pipeline and the reindex script so chunk bodies
    never diverge between the two write paths.  *vectors* (aligned with
    *chunks*) attach embeddings when available; None entries stay lexical.
    """
    now = datetime.now(timezone.utc).isoformat()
    actions: list[dict] = []
    for i, chunk in enumerate(chunks):
        body: dict[str, Any] = {
            "resource_id": resource_id,
            "doc_type": doc_type,
            "chunk_no": i,
            "chunk_text": chunk,
            "title": title,
            "author": author,
            "user_id": user_id,
            "visibility": visibility,
            "char_count": len(chunk),
            "audit_status": "library",
            "created_at": now,
            # Placeholder structural index (chunk-level until docparse
            # structure is wired into the resource pipeline).
            "paragraph_index": i,
        }
        if owner_id is not None:
            body["owner_id"] = owner_id
        if tags:
            body["tags"] = tags
        if publish_date is not None:
            body["publish_date"] = publish_date.isoformat()
        if vectors is not None and vectors[i] is not None:
            body["chunk_vector"] = vectors[i]
        actions.append({"index": {"_index": index_name, "_id": f"{resource_id}_{i}"}})
        actions.append(body)
    return actions


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
    plugin_system: Any = None,
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

    content = await _read_bounded(file, MAX_FILE_SIZE)
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

    chunks = _split_chunks(text, overlap=_CHUNK_OVERLAP)
    total_chars = sum(len(c) for c in chunks)
    # Best-effort embeddings: unconfigured/failed batches leave chunks
    # lexical-only (None entries), never blocking ingestion.
    vectors = await embed_chunks(settings, chunks)

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
    actions = build_chunk_actions(
        resource.id,
        chunks,
        doc_type=ext.lstrip("."),
        title=resource.title,
        author=author or "",
        user_id=owner_name,
        visibility=visibility,
        owner_id=owner_id,
        tags=tag_list,
        publish_date=publish_date,
        index_name=settings.es_index_chunks,
        vectors=vectors,
    )

    try:
        await asyncio.to_thread(bulk_index_chunks, actions)
    except Exception as exc:
        logger.exception("ES bulk index failed for resource %s", resource.id)
        async with db.session() as session:
            await resource_repo.delete(session, resource.id)
            await session.commit()
        # Details (hosts, index names, mapper errors) stay in the server
        # log — the raw ES exception must not reach the client.
        raise HTTPException(
            502, "Elasticsearch 索引写入失败，请稍后重试或联系管理员"
        ) from exc

    # Fresh chunks must be searchable immediately: ask live plugins to drop
    # their coarse-result caches (the search plugin's TTL is only the
    # fallback for a missed broadcast).  Advisory only — never fails the
    # ingest.
    if plugin_system is not None:
        try:
            await plugin_system.broadcast("search.cache_clear")
        except Exception:
            logger.warning("cache invalidation broadcast failed", exc_info=True)

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


# -- PDF preview conversion ---------------------------------------------------


#: Preview PDF cache key inside the resource bucket (per original md5).
def _preview_pdf_key(resource: ResourceTable) -> str:
    return f"{resource.md5}/preview.pdf"


_SOFFICE_CANDIDATES = (
    "/usr/lib/libreoffice/program/soffice",
    "/usr/bin/soffice",
    "/opt/libreoffice/program/soffice",
)


def _find_soffice() -> str | None:
    """Locate a LibreOffice binary for DOCX→PDF conversion."""
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_docx_to_pdf_via_soffice(src: Path, outdir: Path) -> Path | None:
    """Convert a DOCX to PDF with LibreOffice headless; None on any failure."""
    soffice = _find_soffice()
    if soffice is None:
        return None
    try:
        subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(outdir),
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        logger.warning("LibreOffice DOCX→PDF conversion failed for %s", src, exc_info=True)
        return None
    pdf_path = outdir / f"{src.stem}.pdf"
    return pdf_path if pdf_path.exists() else None


def _text_to_pdf_bytes(text: str) -> bytes:
    """Render plain text into a PDF via PyMuPDF (works for any script, CJK-safe)."""
    import fitz

    margin = 50
    fontsize = 10.5
    line_height = fontsize * 1.4
    max_width = fitz.paper_rect("a4").width - 2 * margin
    max_height = fitz.paper_rect("a4").height - 2 * margin

    doc = fitz.open()
    page = doc.new_page()
    y = margin
    for raw_line in text.split("\n"):
        line = raw_line.strip() or " "
        while line:
            width = fitz.get_text_length(line, fontname="china-s", fontsize=fontsize)
            if width > max_width:
                # Binary-search the longest prefix that fits, then wrap.
                lo, hi = 0, len(line)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if (
                        fitz.get_text_length(line[:mid], fontname="china-s", fontsize=fontsize)
                        <= max_width
                    ):
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == 0:
                    lo = 1
                line, rest = line[:lo], line[lo:]
            else:
                rest = ""
            page.insert_text((margin, y), line, fontsize=fontsize, fontname="china-s")
            line = rest
            y += line_height
            if y > max_height:
                page = doc.new_page()
                y = margin
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _resource_pdf_bytes(resource: ResourceTable, bucket: str) -> bytes:
    """Return PDF bytes for a resource — original for PDFs, converted otherwise."""
    if resource.file_type == "pdf":
        return get_object(bucket, resource.minio_path)
    if resource.file_type == "docx":
        raw = get_object(bucket, resource.minio_path)
        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            src = tmp_dir / "source.docx"
            src.write_bytes(raw)
            pdf_path = _convert_docx_to_pdf_via_soffice(src, tmp_dir)
            if pdf_path is not None:
                return pdf_path.read_bytes()
            # Fallback: text-layout PDF from the extracted paragraphs.
            return _text_to_pdf_bytes(_extract_text(src))
    # txt / md — text-layout PDF.
    raw = get_object(bucket, resource.minio_path)
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{resource.file_type}") as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        return _text_to_pdf_bytes(_extract_text(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)


async def get_resource_pdf(
    db: AsyncDatabase,
    settings: Any,
    resource_id: int,
    *,
    owner_id: int | None = None,
    is_admin: bool = False,
) -> tuple[str, bool, str | None]:
    """Resolve a resource to a viewable PDF URL (converting + caching as needed).

    Returns ``(url, converted, original_url)`` where ``original_url`` points
    at the uploaded original file (same as ``url`` for PDFs).  Raises
    404/403 like ``delete_resource``.  Requires MinIO: the original files
    live there and the converted PDF is cached back (``{md5}/preview.pdf``)
    so repeated views skip conversion.
    """
    if not settings.minio_endpoint:
        raise HTTPException(503, "文件存储未配置，无法预览")
    async with db.session() as session:
        resource = await resource_repo.get(session, resource_id)
        if resource is None:
            raise HTTPException(404, "资源不存在")
        if not is_admin:
            if resource.visibility != "public" and (
                resource.visibility != "personal" or resource.owner_id != owner_id
            ):
                raise HTTPException(403, "无权访问该资源")
        if resource.status != "ready":
            raise HTTPException(409, "资源尚未就绪，无法预览")

    bucket = settings.minio_bucket_resources
    if resource.file_type == "pdf":
        pdf_key = resource.minio_path
        converted = False
    else:
        pdf_key = _preview_pdf_key(resource)
        converted = True

    if not await asyncio.to_thread(object_exists, bucket, pdf_key):
        try:
            pdf_bytes = await asyncio.to_thread(_resource_pdf_bytes, resource, bucket)
        except HTTPException:
            raise
        except Exception:
            logger.error(
                "PDF preview generation failed for resource %s", resource_id, exc_info=True
            )
            raise HTTPException(503, "PDF 预览生成失败")
        try:
            await asyncio.to_thread(put_object, bucket, pdf_key, pdf_bytes, "application/pdf")
        except Exception:
            logger.warning(
                "PDF preview cache upload failed for resource %s", resource_id, exc_info=True
            )
    url = await asyncio.to_thread(get_presigned_url, bucket, pdf_key)
    original_url = (
        url
        if resource.file_type == "pdf"
        else await asyncio.to_thread(get_presigned_url, bucket, resource.minio_path)
    )
    return url, converted, original_url


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
        # ES chunks are the searchable payload: delete them while the
        # request can still fail.  A row that outlives its chunks is
        # recoverable by retrying the delete; leftover chunks after the
        # row is gone would keep the content searchable forever.
        try:
            await asyncio.to_thread(delete_by_resource_id, resource_id)
        except Exception as exc:
            logger.warning(
                "ES chunk deletion failed for resource %s", resource_id, exc_info=True
            )
            raise HTTPException(502, "Elasticsearch 切片删除失败，请稍后重试") from exc
        await resource_repo.delete(session, resource_id)
        await session.commit()

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
