"""Profile routes — get/update current user profile."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_db, get_db_session, user_repo
from ..middleware.auth import get_current_user, hash_password, verify_password
from ..rate_limiter import limiter
from courtier.db.tables.user import UserTable, ProfileUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile", tags=["profile"])

profile_security = HTTPBearer(auto_error=False)


def _profile_to_dict(user: UserTable) -> dict:
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


@router.get("")
@limiter.limit("30/minute")
async def get_profile(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(profile_security),
):
    """获取当前用户个人信息。"""
    payload = await get_current_user(request, credentials)
    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.id == payload["uid"])
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(404, "用户不存在")
        return _profile_to_dict(user)


class ProfileUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=128, description="邮箱地址")
    current_password: str | None = Field(default=None, description="当前密码（修改密码时必填）")
    new_password: str | None = Field(default=None, min_length=8, max_length=128, description="新密码")


@router.patch("")
@limiter.limit("30/minute")
async def update_profile(
    request: Request,
    body: ProfileUpdateRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(profile_security),
):
    """更新当前用户个人信息（邮箱、密码）。"""
    payload = await get_current_user(request, credentials)
    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.id == payload["uid"])
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(404, "用户不存在")

        update_data = {}
        if body.email is not None:
            update_data["email"] = body.email

        if body.new_password is not None:
            if not body.current_password:
                raise HTTPException(400, "修改密码时需要提供当前密码")
            if not verify_password(body.current_password, user.password_hash):
                raise HTTPException(400, "当前密码错误")
            update_data["password_hash"] = hash_password(body.new_password)

        if not update_data:
            raise HTTPException(400, "没有提供需要更新的字段")

        updated = await user_repo.update(session, user.id, ProfileUpdate(**update_data))
        return _profile_to_dict(updated)
