"""First-run setup wizard API.

``GET /api/setup/status`` reports whether the wizard is needed (DB mode
and no admin user yet); ``POST /api/setup/admin`` creates the first
admin — open exactly once, only while no admin exists.  In staging /
production the create call additionally requires either a request from
a private network or the one-time ``COURTIER_SETUP_KEY``.

No JWT: there is nobody to authenticate before the first admin exists.
The setup gate middleware (setup_gate.py) keeps these routes reachable.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class CreateAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: str = Field(default="", max_length=128)
    setup_key: str = Field(default="", description="生产模式下的一次性向导令牌")


def _db_mode(request: Request) -> bool:
    return getattr(request.app.state, "settings_store", None) is not None


async def _no_admin_exists(request: Request) -> bool:
    from ..setup_gate import admin_exists

    return not await admin_exists()


def _from_private_network(request: Request) -> bool:
    # client_ip applies the trusted_proxies setting: behind a reverse proxy
    # the direct peer is the proxy's private IP for every caller, so the
    # real client address comes from X-Forwarded-For when configured.
    from ..rate_limiter import client_ip

    host = client_ip(request)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def _production_gate(request: Request, body: CreateAdminRequest) -> None:
    settings = request.app.state.settings
    if settings.deployment_env not in ("staging", "production"):
        return
    expected = os.getenv("COURTIER_SETUP_KEY", "")
    if expected:
        # A configured key is authoritative and required regardless of the
        # request source: behind a reverse proxy every client appears as the
        # proxy's private IP, so a source check must never bypass the key.
        if hmac.compare_digest(expected.encode(), (body.setup_key or "").encode()):
            return
        raise HTTPException(
            403, "生产环境的首启向导需携带正确的 COURTIER_SETUP_KEY"
        )
    if _from_private_network(request):
        return
    raise HTTPException(
        403,
        "生产环境的首启向导仅允许内网来源，或配置并携带 COURTIER_SETUP_KEY",
    )


@router.get("/status")
async def setup_status(request: Request):
    """Whether the first-run wizard is required (unauthenticated)."""
    if not _db_mode(request):
        return {"setup_required": False}
    required = await _no_admin_exists(request)
    return {"setup_required": required}


@router.post("/admin")
async def create_admin(request: Request, body: CreateAdminRequest):
    """Create the first admin user.  Exactly once: refused once any admin
    exists, in any environment."""
    if not _db_mode(request):
        raise HTTPException(409, "env-only 模式无需向导（dev 回退登录由 env 提供）")
    if not _USERNAME_RE.match(body.username):
        raise HTTPException(422, "用户名仅允许字母、数字与 _ . -（3-64 位）")

    from sqlalchemy import select

    from courtier.db.tables.user import UserRole, UserStatus, UserTable

    from ..db import get_db
    from ..middleware.auth import hash_password

    _production_gate(request, body)

    db = get_db()
    async with db.session() as session:
        existing = (
            await session.execute(
                select(UserTable.id).where(UserTable.role == UserRole.admin).limit(1)
            )
        ).scalar()
        if existing is not None:
            raise HTTPException(409, "管理员已存在，向导已关闭")
        user = UserTable(
            username=body.username,
            password_hash=hash_password(body.password),
            email=body.email,
            role=UserRole.admin,
            status=UserStatus.active,
        )
        session.add(user)

    # Open the production API surface immediately (setup gate flag).
    request.app.state._has_admin = True
    logger.info("first admin created via setup wizard: %s", body.username)
    return {"ok": True, "username": body.username}
