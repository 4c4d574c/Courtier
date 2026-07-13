"""Admin user management routes — require admin role."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_db, get_db_session, user_repo
from ..middleware.auth import get_current_user, hash_password, verify_jwt
from ..rate_limiter import limiter
from courtier.db.tables.user import UserTable, UserRole, UserStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

admin_security = HTTPBearer(auto_error=False)


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(admin_security),
) -> dict:
    """FastAPI dependency: verify JWT and check admin role."""
    payload = await get_current_user(request, credentials)
    if payload.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return payload


class UpdateUserRequest(BaseModel):
    role: str | None = Field(default=None, description="角色：admin / auditor")
    status: str | None = Field(default=None, description="状态：active / disabled / pending")
    password: str | None = Field(default=None, min_length=8, max_length=128, description="新密码")
    email: str | None = Field(default=None, max_length=128, description="邮箱地址")


def _user_to_dict(user: UserTable) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "status": user.status.value,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


@router.get("/users")
@limiter.limit("60/minute")
async def list_users(
    request: Request,
    skip: int = 0,
    limit: int = 50,
    admin: dict = Depends(require_admin),
):
    """获取用户列表（分页）。"""
    db = get_db()
    async with db.session() as session:
        users = await user_repo.list(session, skip=skip, limit=limit, order_by="id")
        total = await user_repo.count(session)
        return {
            "items": [_user_to_dict(u) for u in users],
            "total": total,
            "skip": skip,
            "limit": limit,
        }


@router.get("/users/{user_id}")
async def get_user(
    user_id: int,
    admin: dict = Depends(require_admin),
):
    """获取单个用户详情。"""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise HTTPException(404, "用户不存在")
        return _user_to_dict(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    admin: dict = Depends(require_admin),
):
    """管理员更新用户信息（角色、状态、密码、邮箱）。"""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise HTTPException(404, "用户不存在")

        update_data = {}
        if body.role is not None:
            try:
                update_data["role"] = UserRole(body.role)
            except ValueError:
                raise HTTPException(400, f"无效角色: {body.role}")
        if body.status is not None:
            try:
                update_data["status"] = UserStatus(body.status)
            except ValueError:
                raise HTTPException(400, f"无效状态: {body.status}")
        if body.password is not None:
            update_data["password_hash"] = hash_password(body.password)
        if body.email is not None:
            update_data["email"] = body.email

        if not update_data:
            raise HTTPException(400, "没有提供需要更新的字段")

        from courtier.db.tables.user import UserUpdate
        updated = await user_repo.update(session, user_id, UserUpdate(**update_data))
        return _user_to_dict(updated)


@router.get("/approvals")
@limiter.limit("60/minute")
async def list_approvals(
    request: Request,
    skip: int = 0,
    limit: int = 50,
    admin: dict = Depends(require_admin),
):
    """获取待审批用户列表（status=pending）。"""
    db = get_db()
    async with db.session() as session:
        users = await user_repo.list(session, skip=skip, limit=limit, status=UserStatus.pending.value, order_by="id")
        total = await user_repo.count(session, status=UserStatus.pending.value)
        return {
            "items": [_user_to_dict(u) for u in users],
            "total": total,
            "skip": skip,
            "limit": limit,
        }


@router.post("/approvals/{user_id}/approve")
async def approve_user(
    user_id: int,
    admin: dict = Depends(require_admin),
):
    """审批通过用户注册。"""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise HTTPException(404, "用户不存在")
        if user.status != UserStatus.pending:
            raise HTTPException(400, "用户当前状态不允许审批通过")
        from courtier.db.tables.user import UserUpdate
        await user_repo.update(session, user_id, UserUpdate(status=UserStatus.active))
        return {"message": f"用户 {user.username} 已通过审批"}


@router.post("/approvals/{user_id}/reject")
async def reject_user(
    user_id: int,
    admin: dict = Depends(require_admin),
):
    """拒绝用户注册。"""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise HTTPException(404, "用户不存在")
        if user.status != UserStatus.pending:
            raise HTTPException(400, "用户当前状态不允许拒绝")
        from courtier.db.tables.user import UserUpdate
        await user_repo.update(session, user_id, UserUpdate(status=UserStatus.disabled))
        return {"message": f"用户 {user.username} 已被拒绝"}
