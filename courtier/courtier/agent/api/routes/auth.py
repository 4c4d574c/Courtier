"""认证路由 — 注册、登录、刷新、登出。"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from courtier.agent.api.ui_errors import UiError
from courtier.config import get_settings
from courtier.db.tables.refresh_token import RefreshTokenTable
from courtier.db.tables.user import UserRole, UserStatus, UserTable

from ..db import get_db
from ..middleware.auth import (
    create_access_token,
    generate_refresh_token,
    rotate_refresh_token,
)
from ..rate_limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
ACCESS_COOKIE = "access_token"
ACCESS_EXPIRE = max(
    60, int(get_settings().jwt_access_expire_seconds)
)  # 15 minutes — hot-editable via the admin UI


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_\-\.@]+$')
    password: str = Field(..., min_length=8, max_length=128)
    email: str = Field(default="", max_length=128)


def _is_secure_request(request: Request) -> bool:
    """Whether the request came over HTTPS (directly or via reverse proxy).

    Uses X-Forwarded-Proto when present so local HTTP development does not
    drop Secure cookies even if DEPLOYMENT_ENVIRONMENT is misconfigured.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    return forwarded_proto == "https" or (
        not forwarded_proto and request.url.scheme == "https"
    )


def _set_refresh_cookie(
    request: Request, response: Response, raw_token: str, expires_at
) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="strict",
        path="/api/auth",
        expires=expires_at,
    )


def _set_access_cookie(request: Request, response: Response, token: str) -> None:
    """Set the short-lived access token as an httpOnly cookie.

    This is the preferred credential channel for EventSource/SSE clients,
    which cannot set an Authorization header — it keeps the token out of
    URLs (browser history, access logs, proxies). SameSite=Strict keeps
    CSRF exposure equivalent to the header-only flow.
    """
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="strict",
        path="/api",
        max_age=ACCESS_EXPIRE,
    )


def _user_dict(user: UserTable) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "status": user.status.value,
    }


def _fallback_login(body: LoginRequest, settings) -> str:
    """Settings-based admin login for no-database environments (e.g. tests).

    This is intentionally restricted to development/test deployments. In
    staging/production the database must be configured.
    """
    if getattr(settings, "deployment_env", "development") in ("staging", "production"):
        raise HTTPException(
            500,
            "数据库未配置，但在生产/预发布环境不允许使用环境变量管理员登录",
        )
    admin_user = getattr(settings, "admin_user", "admin")
    admin_password = getattr(settings, "admin_password", "")
    if not admin_password:
        raise HTTPException(500, "ADMIN_PASSWORD 未配置，请在 .env 中设置")
    if not hmac.compare_digest(body.username, admin_user) or not hmac.compare_digest(
        body.password, admin_password
    ):
        raise HTTPException(401, "用户名或密码错误")
    logger.warning("Using no-DB fallback admin login (development only)")
    # Return the admin username on success
    return admin_user


@router.post("/register")
@limiter.limit("3/hour")
async def register(request: Request, body: RegisterRequest):
    """注册新用户，status=pending 等待管理员审批。"""
    from ..services import user_service

    try:
        user_id = await user_service.create_pending_user(
            get_db(),
            username=body.username,
            password=body.password,
            email=body.email,
        )
    except user_service.UsernameTaken:
        raise UiError(409, "auth.username_taken")
    return {"message": "注册成功，请等待管理员审批", "user_id": user_id}


def _fallback_login_response(admin_username: str, settings) -> dict:
    """Build login response for settings-based admin auth (no database)."""
    access_token = create_access_token(
        admin_username,
        0,
        UserRole.admin.value,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expire_seconds=ACCESS_EXPIRE,
    )
    return {
        "token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_EXPIRE,
        "user": {
            "id": 0,
            "username": admin_username,
            "email": "",
            "role": UserRole.admin.value,
            "status": UserStatus.active.value,
        },
    }


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    """登录 — 返回 access token + 设置 httpOnly refresh cookie。

    当数据库不可用或用户表未迁移时，自动回退到 settings-based admin 登录
    （保持与旧测试环境的兼容性，且仅允许 development 环境）。
    """
    settings = request.app.state.settings

    # No database configured: settings-based admin login for tests/legacy only.
    # When a database is configured we never fall back to environment-variable
    # credentials, because that bypasses password hashing and account status.
    if not settings.mysql_url:
        admin_username = _fallback_login(body, settings)
        payload = _fallback_login_response(admin_username, settings)
        _set_access_cookie(request, response, payload["token"])
        return payload

    # Credential check + login bookkeeping live in the user service.
    from ..services import user_service

    try:
        user = await user_service.authenticate(
            get_db(), username=body.username, password=body.password
        )
    except user_service.AccountLocked as exc:
        raise UiError(403, "auth.account_locked", seconds=exc.remaining_seconds)
    except user_service.AccountNotApproved:
        raise UiError(403, "auth.pending_approval")
    except user_service.AccountDisabled:
        raise UiError(403, "auth.disabled")
    except user_service.InvalidCredentials:
        raise UiError(401, "auth.invalid_credentials")

    access_token = create_access_token(
        user.username,
        user.id,
        user.role.value,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expire_seconds=ACCESS_EXPIRE,
    )
    raw_refresh, token_hash, expires_at = generate_refresh_token()
    await user_service.record_successful_login(get_db(), user, token_hash, expires_at)
    _set_refresh_cookie(request, response, raw_refresh, expires_at)
    _set_access_cookie(request, response, access_token)

    return {
        "token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_EXPIRE,
        "user": _user_dict(user),
    }


@router.post("/refresh")
@limiter.limit("30/minute")
async def refresh(request: Request, response: Response):
    """用 refresh cookie 换取新的 access token。"""
    settings = request.app.state.settings
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if not raw_token:
        raise HTTPException(401, "缺少 refresh token")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db = get_db()
    async with db.session() as session:
        result = await rotate_refresh_token(token_hash, session)
        if result is None:
            raise HTTPException(401, "refresh token 无效或已过期")
        new_raw, new_expires_at, user_id = result

        user_result = await session.execute(
            select(UserTable).where(UserTable.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise HTTPException(401, "refresh token 关联用户不存在")

        if user.status != UserStatus.active:
            raise HTTPException(403, "账号已被禁用或未通过审批")

        access_token = create_access_token(
            user.username,
            user.id,
            user.role.value,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expire_seconds=ACCESS_EXPIRE,
        )
        _set_refresh_cookie(request, response, new_raw, new_expires_at)
        _set_access_cookie(request, response, access_token)

        return {
            "token": access_token,
            "token_type": "bearer",
            "expires_in": ACCESS_EXPIRE,
            "user": _user_dict(user),
        }


@router.post("/logout")
@limiter.limit("10/minute")
async def logout(request: Request, response: Response):
    """登出 — 撤销 refresh token，清除 cookie。"""
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if raw_token:
        db = get_db()
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with db.session() as session:
            result = await session.execute(
                select(RefreshTokenTable).where(
                    RefreshTokenTable.token_hash == token_hash
                )
            )
            rt = result.scalar_one_or_none()
            if rt:
                rt.revoked = True
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    response.delete_cookie(ACCESS_COOKIE, path="/api")
    return {"message": "已登出"}
