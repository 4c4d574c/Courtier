"""JWT 认证中间件和依赖注入。"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from courtier.config import Settings

logger = logging.getLogger(__name__)

# Allowed JWT signing algorithms. Keep this list minimal and explicit to prevent
# algorithm-confusion and ``none``-algorithm attacks.
ALLOWED_JWT_ALGORITHMS: frozenset[str] = frozenset({"HS256"})

security = HTTPBearer(auto_error=False)


def _is_admin(payload: dict) -> bool:
    """Return True if the JWT payload represents an admin user."""
    return payload.get("role") == "admin"


def get_jwt_secret(settings: "Settings") -> str:
    """返回 JWT 密钥，未配置时抛出明确错误。"""
    secret = getattr(settings, "jwt_secret", "")
    if not secret:
        raise HTTPException(
            500,
            "JWT_SECRET 未配置，请在 .env 中设置 JWT_SECRET",
        )
    return secret


def create_token(
    username: str,
    secret: str,
    algorithm: str = "HS256",
    expire_seconds: int = 86400,
) -> str:
    """创建 JWT token。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now.timestamp() + expire_seconds,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def verify_token(token: str, secret: str, algorithm: str = "HS256") -> dict[str, Any]:
    """验证 JWT token，返回 payload。验证失败抛出 HTTPException。"""
    if algorithm not in ALLOWED_JWT_ALGORITHMS:
        raise HTTPException(500, "不支持的 JWT 签名算法")
    try:
        payload = jwt.decode(
            token, secret, algorithms=[algorithm], options={"require": ["exp"]}
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token 无效")
    return payload


def _resolve_token(
    settings: "Settings",
    credentials: HTTPAuthorizationCredentials | None,
    query_token: str | None,
    cookie_token: str | None = None,
) -> dict[str, Any]:
    """Resolve and verify a JWT token from header, query param, or cookie.

    Supports three token transfer methods:
    1. Authorization: Bearer <token> header (standard HTTP)
    2. ?token=<token> query param (legacy SSE clients)
    3. access_token httpOnly cookie (EventSource / SSE cannot set headers;
       this is the preferred SSE path — query tokens leak into logs)
    """
    secret = get_jwt_secret(settings)
    token: str | None = None
    if credentials is not None:
        token = credentials.credentials
    elif query_token:
        token = query_token
    elif cookie_token:
        token = cookie_token

    if token is None:
        raise HTTPException(
            401,
            "缺少认证信息，请在 Authorization header 中提供 Bearer token，"
            "或通过 ?token= 查询参数传递",
        )

    algorithm = getattr(settings, "jwt_algorithm", "HS256")
    if algorithm not in ALLOWED_JWT_ALGORITHMS:
        raise HTTPException(500, "不支持的 JWT 签名算法")
    return verify_token(token, secret, algorithm=algorithm)


async def verify_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """FastAPI 依赖：验证 JWT 并返回用户名。"""
    payload = _resolve_token(
        request.app.state.settings,
        credentials,
        request.query_params.get("token"),
        request.cookies.get("access_token"),
    )
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise HTTPException(401, "Token 中缺少有效的 sub 字段")
    # Stamped for the rate limiter's per-user bucket key (rate_limiter.py).
    # This router-level dependency covers every /api route; endpoint-level
    # get_current_user stamps the same value again where declared.
    request.state.auth_user = sub
    return sub


def hash_password(password: str, rounds: int | None = None) -> str:
    if rounds is None:
        from courtier.config import get_settings
        rounds = get_settings().bcrypt_rounds
    return cast(str, bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode())


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(
    username: str, user_id: int, role: str,
    secret: str, algorithm: str = "HS256", expire_seconds: int = 900,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username, "uid": user_id, "role": role,
        "iat": now, "exp": now.timestamp() + expire_seconds,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def generate_refresh_token() -> tuple[str, str, datetime]:
    raw = secrets.token_hex(64)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    return raw, token_hash, expires_at


async def rotate_refresh_token(old_hash: str, session) -> tuple[str, datetime, int] | None:
    from sqlalchemy import select, update

    from courtier.db.tables.refresh_token import RefreshTokenTable

    result = await session.execute(
        select(RefreshTokenTable).where(
            RefreshTokenTable.token_hash == old_hash,
        )
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        return None
    if stored.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None

    # Refresh-token reuse detection: if this token was already revoked, an
    # attacker may have used it. Revoke the whole user's token family and
    # reject the request so the legitimate user is forced to re-authenticate.
    if stored.revoked:
        await session.execute(
            update(RefreshTokenTable)
            .where(RefreshTokenTable.user_id == stored.user_id)
            .values(revoked=True)
        )
        return None

    stored.revoked = True
    user_id = stored.user_id
    raw, token_hash, expires_at = generate_refresh_token()
    session.add(RefreshTokenTable(user_id=user_id, token_hash=token_hash, expires_at=expires_at))
    return raw, expires_at, user_id


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """FastAPI dependency: returns full user claims dict {sub, uid, role}."""
    payload = _resolve_token(
        request.app.state.settings,
        credentials,
        request.query_params.get("token"),
        request.cookies.get("access_token"),
    )
    # Stamped for the rate limiter's per-user bucket key (rate_limiter.py):
    # dependencies resolve before the endpoint's limiter decorator runs.
    sub = payload.get("sub")
    if isinstance(sub, str) and sub:
        request.state.auth_user = sub
    return payload
