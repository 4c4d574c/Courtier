"""Account deletion routes — self-service requests + admin execution.

Self flow: password-verified request → admin approval → pipeline run.
The requester stays active (and may cancel) until the pipeline executes.
Admin flow: pick a user + confirm → immediate execution.  Admin accounts
are not deletable through either entry.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.tables import (
    REQUEST_CANCELLED,
    REQUEST_EXECUTED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    DeletionRequestTable,
    UserTable,
)
from courtier.db.tables.base import utcnow

from ..db import get_db, get_db_session
from ..middleware.auth import _is_admin, get_current_user, verify_password
from ..rate_limiter import limiter
from ..services.account_deletion_service import execute_account_deletion
from .admin_users import require_admin

router = APIRouter(prefix="/api/profile/deletion-request", tags=["profile"])
admin_router = APIRouter(prefix="/api/admin/deletion-requests", tags=["admin"])
admin_users_router = APIRouter(prefix="/api/admin", tags=["admin"])


class DeletionRequestBody(BaseModel):
    password: str = Field(min_length=1)


def _request_dict(row: DeletionRequestTable) -> dict:
    return {
        "id": row.id,
        "userId": row.user_id,
        "status": row.status,
        "requestedBy": row.requested_by,
        "decidedBy": row.decided_by,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "decidedAt": row.decided_at.isoformat() if row.decided_at else None,
    }


async def _run_pipeline(
    request: Request, *, user_id: int, username: str, trigger: str, executed_by: str
) -> dict:
    result = await execute_account_deletion(
        get_db(),
        request.app.state.settings,
        request.app.state.session_store,
        request.app.state.file_store,
        user_id=user_id,
        username=username,
        trigger=trigger,
        executed_by=executed_by,
        run_manager=getattr(request.app.state, "run_manager", None),
    )
    return result.to_counts_dict()


# ------------------------------------------------------------------ self flow


@router.post("")
@limiter.limit("5/minute")
async def submit_deletion_request(
    request: Request,
    body: DeletionRequestBody,
    session: AsyncSession = Depends(get_db_session),
    current_user_payload: dict = Depends(get_current_user),
):
    """提交注销申请：密码验证；admin 账号不可注销；重复申请 409。"""
    uid = int(current_user_payload.get("uid") or 0)
    username = str(current_user_payload.get("sub") or "")
    user = await session.get(UserTable, uid)
    if user is None or user.username != username:
        raise HTTPException(404, "用户不存在")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "密码错误")
    if _is_admin(current_user_payload) or user.role.value == "admin":
        raise HTTPException(403, "管理员账号不可注销；如需删除请先由其他管理员降级")
    existing = (
        await session.execute(
            select(DeletionRequestTable).where(
                DeletionRequestTable.user_id == uid,
                DeletionRequestTable.status == REQUEST_PENDING,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "已存在待审核的注销申请")
    row = DeletionRequestTable(user_id=uid, status=REQUEST_PENDING, requested_by="self")
    session.add(row)
    await session.commit()
    return _request_dict(row)


@router.get("")
@limiter.limit("60/minute")
async def get_my_deletion_request(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user_payload: dict = Depends(get_current_user),
):
    uid = int(current_user_payload.get("uid") or 0)
    row = (
        await session.execute(
            select(DeletionRequestTable)
            .where(
                DeletionRequestTable.user_id == uid,
                DeletionRequestTable.status == REQUEST_PENDING,
            )
            .order_by(DeletionRequestTable.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"request": None}
    return {"request": _request_dict(row)}


@router.delete("")
@limiter.limit("10/minute")
async def cancel_my_deletion_request(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user_payload: dict = Depends(get_current_user),
):
    uid = int(current_user_payload.get("uid") or 0)
    row = (
        await session.execute(
            select(DeletionRequestTable).where(
                DeletionRequestTable.user_id == uid,
                DeletionRequestTable.status == REQUEST_PENDING,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "没有待审核的注销申请")
    row.status = REQUEST_CANCELLED
    row.decided_at = utcnow()
    await session.commit()
    return {"cancelled": row.id}


# ----------------------------------------------------------------- admin flow


@admin_router.get("")
@limiter.limit("60/minute")
async def list_pending_deletion_requests(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: dict = Depends(require_admin),
):
    rows = (
        (
            await session.execute(
                select(DeletionRequestTable)
                .where(DeletionRequestTable.status == REQUEST_PENDING)
                .order_by(DeletionRequestTable.id.asc())
            )
        )
        .scalars()
        .all()
    )
    items = []
    for row in rows:
        item = _request_dict(row)
        user = await session.get(UserTable, row.user_id)
        item["username"] = user.username if user else None
        item["userStatus"] = user.status.value if user else None
        items.append(item)
    return {"items": items}


@admin_router.post("/{request_id}/approve")
@limiter.limit("10/minute")
async def approve_deletion_request(
    request: Request,
    request_id: int,
    session: AsyncSession = Depends(get_db_session),
    admin: dict = Depends(require_admin),
):
    """批准注销申请并立即执行清除管线。"""
    row = await session.get(DeletionRequestTable, request_id)
    if row is None or row.status != REQUEST_PENDING:
        raise HTTPException(404, "申请不存在或不在待审核状态")
    user = await session.get(UserTable, row.user_id)
    if user is None:
        raise HTTPException(404, "目标用户不存在")
    if user.role.value == "admin":
        raise HTTPException(400, "管理员账号不可注销")
    receipt = await _run_pipeline(
        request,
        user_id=row.user_id,
        username=user.username,
        trigger="self_approved",
        executed_by=str(admin.get("sub") or ""),
    )
    await session.refresh(row)
    return {"receipt": receipt, "request": _request_dict(row)}


@admin_router.post("/{request_id}/reject")
@limiter.limit("30/minute")
async def reject_deletion_request(
    request: Request,
    request_id: int,
    session: AsyncSession = Depends(get_db_session),
    admin: dict = Depends(require_admin),
):
    row = await session.get(DeletionRequestTable, request_id)
    if row is None or row.status != REQUEST_PENDING:
        raise HTTPException(404, "申请不存在或不在待审核状态")
    row.status = REQUEST_REJECTED
    row.decided_by = str(admin.get("sub") or "")
    row.decided_at = utcnow()
    await session.commit()
    return _request_dict(row)


@admin_users_router.delete("/users/{user_id}")
@limiter.limit("10/minute")
async def admin_delete_user_account(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_db_session),
    admin: dict = Depends(require_admin),
):
    """管理员直接注销：前端选人 + 二次确认后调用；admin 目标 400。"""
    user = await session.get(UserTable, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    if user.role.value == "admin":
        raise HTTPException(400, "管理员账号不可注销；如需删除请先降级为 auditor")
    username = user.username
    receipt = await _run_pipeline(
        request,
        user_id=user_id,
        username=username,
        trigger="admin",
        executed_by=str(admin.get("sub") or ""),
    )
    return {"receipt": receipt}
