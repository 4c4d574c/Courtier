"""Resource library routes — upload documents into the ES chunks index."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from ..db import get_db
from ..middleware.auth import _is_admin, get_current_user
from ..rate_limiter import limiter
from ..services.resource_service import (
    delete_resource,
    get_resource_pdf,
    ingest_resource,
    list_resources,
)

router = APIRouter(prefix="/api/resources", tags=["resources"])


def _require_db(request: Request):
    if not request.app.state.settings.mysql_url:
        raise HTTPException(503, "数据库未配置，无法使用资源库")
    return get_db()


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_resource(
    request: Request,
    file: UploadFile,
    title: str = Form(""),
    author: str = Form(""),
    source: str = Form(""),
    tags: str = Form(""),
    publish_date: date | None = Form(None),
    visibility: str = Form("public"),
    current_user_payload: dict = Depends(get_current_user),
):
    """上传文档到资源库：普通用户入个人库，管理员可选公开或个人。"""
    db = _require_db(request)
    resource = await ingest_resource(
        file,
        title=title.strip(),
        author=author.strip(),
        source=source.strip(),
        tags=tags.strip(),
        publish_date=publish_date,
        owner_name=current_user_payload.get("sub", ""),
        owner_id=current_user_payload.get("uid"),
        is_admin=_is_admin(current_user_payload),
        visibility=visibility,
        settings=request.app.state.settings,
        db=db,
        plugin_system=getattr(request.app.state, "plugin_system", None),
    )
    return {
        "id": resource.id,
        "title": resource.title,
        "chunkCount": resource.chunk_count,
        "charCount": resource.char_count,
        "visibility": resource.visibility,
        "status": resource.status,
    }


@router.get("/list")
@limiter.limit("30/minute")
async def list_resources_handler(
    request: Request,
    skip: int = 0,
    limit: int = 50,
    query: str = "",
    scope: str = "all",
    current_user_payload: dict = Depends(get_current_user),
):
    """查询资源库列表：普通用户可见公共库+个人库，管理员可见全部。"""
    db = _require_db(request)
    return await list_resources(
        db,
        skip=skip,
        limit=min(limit, 200),
        query=query,
        owner_id=current_user_payload.get("uid"),
        is_admin=_is_admin(current_user_payload),
        scope=scope,
    )


@router.get("/{resource_id}/pdf")
@limiter.limit("30/minute")
async def get_resource_pdf_handler(
    request: Request,
    resource_id: int,
    current_user_payload: dict = Depends(get_current_user),
):
    """获取资源的 PDF 预览 URL。

    原始文件为 PDF 时直接返回；DOCX / TXT / MD 按需转换（LibreOffice 或
    PyMuPDF 文本版），转换产物缓存到 MinIO 供后续复用。
    """
    db = _require_db(request)
    url, converted, original_url = await get_resource_pdf(
        db,
        request.app.state.settings,
        resource_id,
        owner_id=current_user_payload.get("uid"),
        is_admin=_is_admin(current_user_payload),
    )
    return {"url": url, "converted": converted, "originalUrl": original_url}


@router.delete("/{resource_id}")
@limiter.limit("10/minute")
async def delete_resource_handler(
    resource_id: int,
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """删除资源：本人个人条目或管理员。同时清理 ES 切片与 MinIO 原文件。"""
    db = _require_db(request)
    resource = await delete_resource(
        db,
        request.app.state.settings,
        resource_id,
        owner_id=current_user_payload.get("uid"),
        is_admin=_is_admin(current_user_payload),
    )
    if resource is None:
        raise HTTPException(404, "资源不存在")
    return {"deleted": resource_id}
