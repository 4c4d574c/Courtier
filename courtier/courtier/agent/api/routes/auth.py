"""认证路由 — 注册、登录、刷新、登出。"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from courtier.db.tables.refresh_token import RefreshTokenTable
from courtier.db.tables.user import UserRole, UserStatus, UserTable

from ..db import get_db, user_repo
from ..middleware.auth import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    rotate_refresh_token,
    verify_password,
)
from ..rate_limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
ACCESS_COOKIE = "access_token"
ACCESS_EXPIRE = 900  # 15 minutes


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
    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == body.username)
        )
        if result.scalar_one_or_none() is not None:
            raise HTTPException(409, "用户名已存在")
        user = await user_repo.create(
            session,
            {
                "username": body.username,
                "password_hash": hash_password(body.password),
                "email": body.email,
            },
        )
        return {"message": "注册成功，请等待管理员审批", "user_id": user.id}


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

    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == body.username)
        )
        user = result.scalar_one_or_none()

    if user is None:
        # Dummy bcrypt to normalize timing and prevent username enumeration
        _DUMMY_HASH = "$2b$12$LJ3m4ys3GZfnYMz8kVsKaOTSxGHLfEhCgJwN5B6Hm3VlOUlS3wFJq"
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(401, "用户名或密码错误")

    # Check account lockout before verifying password.  The column is a
    # naive DateTime and drivers return naive UTC — normalize before
    # comparing against the aware clock (mirrors middleware/auth.py).
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until is not None and locked_until > now:
        remaining = int((locked_until - now).total_seconds())
        raise HTTPException(403, f"账号已被临时锁定，请在 {remaining} 秒后重试")

    _MAX_FAILED_ATTEMPTS = 10
    _LOCKOUT_DURATION = timedelta(minutes=15)

    if not verify_password(body.password, user.password_hash):
        # Track failed attempt and potentially lock account
        async with db.session() as session:
            merged = await session.merge(user)
            merged.failed_login_attempts += 1
            if merged.failed_login_attempts >= _MAX_FAILED_ATTEMPTS:
                # Naive UTC into the naive DateTime column (see read side).
                merged.locked_until = (now + _LOCKOUT_DURATION).replace(tzinfo=None)
                logger.warning(
                    "Account locked: %s (%d failed attempts)",
                    merged.username,
                    merged.failed_login_attempts,
                )
        raise HTTPException(401, "用户名或密码错误")

    # Verify account status before resetting failure counters so that a
    # pending/disabled account does not have its lockout state cleared.
    if user.status == UserStatus.pending:
        raise HTTPException(403, "账号尚未通过审批，请等待管理员审核")
    if user.status == UserStatus.disabled:
        raise HTTPException(403, "账号已被禁用")

    # Successful login — reset failure counters inside the same transaction
    # that creates the refresh token to keep state consistent.
    access_token = create_access_token(
        user.username,
        user.id,
        user.role.value,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expire_seconds=ACCESS_EXPIRE,
    )
    raw_refresh, token_hash, expires_at = generate_refresh_token()
    async with db.session() as session:
        merged = await session.merge(user)
        if merged.failed_login_attempts > 0 or merged.locked_until is not None:
            merged.failed_login_attempts = 0
            merged.locked_until = None
        session.add(
            RefreshTokenTable(
                user_id=user.id, token_hash=token_hash, expires_at=expires_at
            )
        )
        # Opportunistic hygiene: refresh rows are never read past expiry,
        # and without cleanup the table grows forever.  Sweep expired rows
        # older than a week while we are already in a transaction.
        await session.execute(
            delete(RefreshTokenTable).where(
                RefreshTokenTable.expires_at
                < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
            )
        )
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
