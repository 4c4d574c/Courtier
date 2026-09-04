"""Memory routes — global shared layer + per-user layer management.

``/api/memory/global`` is readable by any authenticated user; writes land
behind ``require_admin`` (403 for everyone else, enforced again in the
service).  ``/api/memory/mine`` is the caller's own user layer.  The audit
trail lives under ``/api/admin/memory/changes`` (admin only).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..middleware.auth import _is_admin, get_current_user
from ..rate_limiter import limiter
from ..services.memory_service import (
    Identity,
    MemoryAccessError,
    MemoryNotFoundError,
    MemoryValidationError,
    clear_layer,
    delete_entry,
    get_entry,
    list_changes,
    list_entries,
    update_entry,
    upsert_entry,
)
from .admin_users import require_admin

router = APIRouter(prefix="/api/memory", tags=["memory"])
admin_router = APIRouter(prefix="/api/admin/memory", tags=["admin"])


class MemoryUpsertRequest(BaseModel):
    title: str = Field(min_length=1, max_length=190)
    content: str = Field(min_length=1)
    domain: str = Field(default="common", max_length=64)


class MemoryUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=190)
    content: str | None = Field(default=None, min_length=1)
    domain: str | None = Field(default=None, max_length=64)


async def _memory_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Per-request session; 503 when the deployment has no MySQL configured."""
    if not request.app.state.settings.mysql_url:
        raise HTTPException(503, "数据库未配置，无法使用记忆功能")
    async with get_db().session() as session:
        yield session


def _identity(payload: dict) -> Identity:
    return Identity(
        owner_id=int(payload.get("uid") or 0),
        is_admin=_is_admin(payload),
        actor=str(payload.get("sub") or ""),
    )


def _known_domains(request: Request) -> set[str]:
    config = getattr(request.app.state, "courtier_config", None)
    return {pkg.name for pkg in getattr(config, "domains", [])}


def _map_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryAccessError):
        return HTTPException(403, str(exc))
    if isinstance(exc, MemoryValidationError):
        return HTTPException(400, str(exc))
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(404, str(exc))
    return HTTPException(500, f"记忆操作失败：{exc}")


@router.get("/domains")
@limiter.limit("60/minute")
async def list_known_domains(
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """已加载领域包名列表（UI 分组选择、agent 写入校验共用）。"""
    return {"domains": sorted(_known_domains(request))}


# ---------------------------------------------------------------- global layer


@router.get("/global")
@limiter.limit("60/minute")
async def list_global(
    request: Request,
    domain: str = "all",
    query: str = "",
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    """全局共享层列表：所有登录用户可读。"""
    try:
        return await list_entries(
            session,
            layer="global",
            identity=_identity(current_user_payload),
            domain=domain,
            query=query,
            skip=skip,
            limit=limit,
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.get("/global/{entry_id}")
@limiter.limit("60/minute")
async def get_global_entry(
    request: Request,
    entry_id: int,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    try:
        return await get_entry(session, entry_id, identity=_identity(current_user_payload))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.post("/global")
@limiter.limit("30/minute")
async def create_global_entry(
    request: Request,
    body: MemoryUpsertRequest,
    session: AsyncSession = Depends(_memory_session),
    admin: dict = Depends(require_admin),
):
    """写入全局共享层（按 title upsert）：仅管理员。"""
    try:
        return await upsert_entry(
            session,
            layer="global",
            title=body.title,
            content=body.content,
            domain=body.domain,
            identity=_identity(admin),
            known_domains=_known_domains(request),
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.patch("/global/{entry_id}")
@limiter.limit("30/minute")
async def patch_global_entry(
    request: Request,
    entry_id: int,
    body: MemoryUpdateRequest,
    session: AsyncSession = Depends(_memory_session),
    admin: dict = Depends(require_admin),
):
    try:
        return await update_entry(
            session,
            entry_id,
            identity=_identity(admin),
            title=body.title,
            content=body.content,
            domain=body.domain,
            known_domains=_known_domains(request),
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.delete("/global/{entry_id}")
@limiter.limit("30/minute")
async def delete_global_entry(
    request: Request,
    entry_id: int,
    session: AsyncSession = Depends(_memory_session),
    admin: dict = Depends(require_admin),
):
    try:
        await delete_entry(session, entry_id, identity=_identity(admin))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc
    return {"deleted": entry_id}


@router.delete("/global")
@limiter.limit("10/minute")
async def clear_global(
    request: Request,
    session: AsyncSession = Depends(_memory_session),
    admin: dict = Depends(require_admin),
):
    """清空全局共享层（逐条审计）。"""
    try:
        count = await clear_layer(session, layer="global", identity=_identity(admin))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc
    return {"cleared": count}


# ------------------------------------------------------------------ user layer


@router.get("/mine")
@limiter.limit("60/minute")
async def list_mine(
    request: Request,
    domain: str = "all",
    query: str = "",
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    """我的用户层记忆列表。"""
    try:
        return await list_entries(
            session,
            layer="user",
            identity=_identity(current_user_payload),
            domain=domain,
            query=query,
            skip=skip,
            limit=limit,
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.get("/mine/{entry_id}")
@limiter.limit("60/minute")
async def get_my_entry(
    request: Request,
    entry_id: int,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    try:
        return await get_entry(session, entry_id, identity=_identity(current_user_payload))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.post("/mine")
@limiter.limit("30/minute")
async def create_my_entry(
    request: Request,
    body: MemoryUpsertRequest,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    """写入我的用户层（按 title upsert）。"""
    try:
        return await upsert_entry(
            session,
            layer="user",
            title=body.title,
            content=body.content,
            domain=body.domain,
            identity=_identity(current_user_payload),
            known_domains=_known_domains(request),
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.patch("/mine/{entry_id}")
@limiter.limit("30/minute")
async def patch_my_entry(
    request: Request,
    entry_id: int,
    body: MemoryUpdateRequest,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    try:
        return await update_entry(
            session,
            entry_id,
            identity=_identity(current_user_payload),
            title=body.title,
            content=body.content,
            domain=body.domain,
            known_domains=_known_domains(request),
        )
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.delete("/mine/{entry_id}")
@limiter.limit("30/minute")
async def delete_my_entry(
    request: Request,
    entry_id: int,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    try:
        await delete_entry(session, entry_id, identity=_identity(current_user_payload))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc
    return {"deleted": entry_id}


@router.delete("/mine")
@limiter.limit("10/minute")
async def clear_mine(
    request: Request,
    session: AsyncSession = Depends(_memory_session),
    current_user_payload: dict = Depends(get_current_user),
):
    """清空我的用户层（逐条审计）。"""
    try:
        count = await clear_layer(session, layer="user", identity=_identity(current_user_payload))
    except (MemoryAccessError, MemoryValidationError, MemoryNotFoundError) as exc:
        raise _map_errors(exc) from exc
    return {"cleared": count}


# ----------------------------------------------------------------------- audit


@admin_router.get("/changes")
@limiter.limit("60/minute")
async def list_memory_changes(
    request: Request,
    entry_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(_memory_session),
    admin: dict = Depends(require_admin),
):
    """记忆审计流水（hash-only）：仅管理员。"""
    return await list_changes(session, entry_id=entry_id, skip=skip, limit=limit)
