"""JWT 认证中间件和依赖注入。"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def get_jwt_secret(settings) -> str:
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


def verify_token(token: str, secret: str, algorithm: str = "HS256") -> dict:
    """验证 JWT token，返回 payload。验证失败抛出 HTTPException。"""
    try:
        payload = jwt.decode(
            token, secret, algorithms=[algorithm], options={"require": ["exp"]}
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token 无效")
    return payload


async def verify_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """FastAPI 依赖：验证 JWT 并返回用户名。

    支持两种 token 传递方式：
    1. Authorization: Bearer <token> header（标准 HTTP）
    2. ?token=<token> query 参数（EventSource / SSE 不支持自定义 headers）
    """
    settings = request.app.state.settings
    secret = get_jwt_secret(settings)

    # Resolve token: header first, then query param (for EventSource).
    token: str | None = None
    if credentials is not None:
        token = credentials.credentials
    elif request.query_params.get("token"):
        token = request.query_params["token"]

    if token is None:
        raise HTTPException(401, "缺少认证信息，请在 Authorization header 中提供 Bearer token，或通过 ?token= 查询参数传递")

    payload = verify_token(
        token,
        secret,
        algorithm=getattr(settings, "jwt_algorithm", "HS256"),
    )
    return payload["sub"]


def hash_password(password: str, rounds: int | None = None) -> str:
    if rounds is None:
        from courtier.config import get_settings
        rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()


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
    from sqlalchemy import select
    from courtier.db.tables.refresh_token import RefreshTokenTable

    result = await session.execute(
        select(RefreshTokenTable).where(
            RefreshTokenTable.token_hash == old_hash,
            RefreshTokenTable.revoked == False,
        )
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        return None
    if stored.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None

    stored.revoked = True
    user_id = stored.user_id
    raw, token_hash, expires_at = generate_refresh_token()
    session.add(RefreshTokenTable(user_id=user_id, token_hash=token_hash, expires_at=expires_at))
    return raw, expires_at, user_id


async def get_current_user(request: Request, credentials) -> dict:
    """FastAPI dependency: returns full user claims dict {sub, uid, role}."""
    settings = request.app.state.settings
    secret = get_jwt_secret(settings)
    token = None
    if credentials is not None:
        token = credentials.credentials
    elif request.query_params.get("token"):
        token = request.query_params["token"]
    if token is None:
        raise HTTPException(401, "缺少认证信息")
    payload = verify_token(token, secret, algorithm=getattr(settings, "jwt_algorithm", "HS256"))
    return payload
