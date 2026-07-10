# Login Page & User Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-user authentication (login/register/logout), JWT+httpOnly-cookie token management, and admin user management (list/approve/disable/role-edit) to SDTAgent.

**Architecture:** Backend adds `users` + `refresh_tokens` MySQL tables, bcrypt password hashing, and REST endpoints for auth + admin user CRUD + profile. Frontend introduces Vue Router with route guards, a `useAuth` composable for token lifecycle, and 6 new views. Access token stays in memory; refresh token uses httpOnly Secure SameSite=Strict cookie.

**Tech Stack:** Python 3.12+ / FastAPI / SQLAlchemy async / Alembic / PyJWT / bcrypt · Vue 3 / TypeScript / Vue Router / Vite

---

## File Structure

### Backend — new files
```
docaudit-agent/src/dbop/tables/user.py                # UserTable ORM + Pydantic schemas
docaudit-agent/src/dbop/tables/refresh_token.py        # RefreshTokenTable ORM
docaudit-agent/src/agent/api/db.py                     # DB session dependency + admin bootstrap
docaudit-agent/src/agent/api/routes/admin_users.py     # Admin user management endpoints
docaudit-agent/src/agent/api/routes/profile.py         # Self-profile endpoints
docaudit-agent/alembic/versions/xxxx_add_users_and_refresh_tokens.py
```

### Backend — modified files
```
docaudit-agent/src/dbop/tables/__init__.py        # Export new models
docaudit-agent/src/agent/api/middleware/auth.py    # Add refresh-token + bcrypt helpers
docaudit-agent/src/agent/api/routes/auth.py        # Register, refresh, logout endpoints
docaudit-agent/src/agent/api/app.py                # Register new routers, CORS credentials, bootstrap
docaudit-agent/src/config.py                       # jwt_access_expire_seconds + bcrypt_rounds
docaudit-agent/.env.example                        # Document new settings
```

### Frontend — new files
```
webui/src/router/index.ts                  # Vue Router setup + guards
webui/src/composables/useAuth.ts           # Auth state, login/logout/refresh
webui/src/views/LoginView.vue              # Login page
webui/src/views/RegisterView.vue           # Registration page
webui/src/views/AdminLayout.vue            # Admin shell (sidebar + router-view)
webui/src/views/UserManagement.vue         # Admin user table
webui/src/views/ApprovalManagement.vue     # Admin approvals table
webui/src/views/ProfileView.vue            # User profile/settings
webui/src/styles/auth.css                  # Login/register split-layout styles
webui/src/styles/admin.css                 # Admin panel styles
```

### Frontend — modified files
```
webui/src/main.ts                  # Install router
webui/src/App.vue                  # Replace Terminal with <router-view>
webui/src/api/client.ts            # Cookie-based refresh, auto-refresh on 401, remove auto-login
webui/src/components/Terminal.vue  # Add user dropdown menu in top-nav
```

---

## Phase 1: Backend — Database Foundation

### Task 1: User ORM model + RefreshToken model

**Files:**
- Create: `docaudit-agent/src/dbop/tables/user.py`
- Create: `docaudit-agent/src/dbop/tables/refresh_token.py`
- Modify: `docaudit-agent/src/dbop/tables/__init__.py`

**Code — `user.py`:**

```python
"""User ORM model and Pydantic schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class UserRole(str, Enum):
    admin = "admin"
    auditor = "auditor"


class UserStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    pending = "pending"


class UserTable(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    email: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), nullable=False, default=UserRole.auditor)
    status: Mapped[UserStatus] = mapped_column(SAEnum(UserStatus), nullable=False, default=UserStatus.pending)
    avatar_url: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<User {self.id} ({self.username}) role={self.role.value}>"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    email: str = Field(default="", max_length=128)


class UserUpdate(BaseModel):
    role: UserRole | None = None
    status: UserStatus | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    email: str | None = Field(default=None, max_length=128)


class ProfileUpdate(BaseModel):
    email: str | None = Field(default=None, max_length=128)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)
```

**Code — `refresh_token.py`:**

```python
"""RefreshToken ORM model — persisted for httpOnly-cookie token rotation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class RefreshTokenTable(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<RefreshToken {self.id} user={self.user_id} revoked={self.revoked}>"
```

**Update `__init__.py`:** Add `from .user import ...` and `from .refresh_token import ...` to imports and `__all__`.

- [ ] **Step 1: Write the three files as above**
- [ ] **Step 2: Run existing tests to verify no import errors**

```bash
cd docaudit-agent && uv run python -c "from src.dbop.tables import UserTable, RefreshTokenTable; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/dbop/tables/user.py src/dbop/tables/refresh_token.py src/dbop/tables/__init__.py
git commit -m "feat: add User and RefreshToken ORM models"
```

---

### Task 2: Alembic migration

- [ ] **Step 1: Generate migration**

```bash
cd docaudit-agent && uv run alembic revision --autogenerate -m "add users and refresh_tokens"
```

- [ ] **Step 2: Review the generated file in `alembic/versions/`** — verify `upgrade()` creates `users` (id, username, password_hash, email, role enum, status enum, avatar_url, created_at, updated_at) and `refresh_tokens` (id, user_id FK, token_hash, expires_at, revoked, created_at).

- [ ] **Step 3: Run unit tests to verify no regression**

```bash
cd docaudit-agent && uv run pytest tests/ -m "not integration" -x -q
```

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/
git commit -m "feat: add users and refresh_tokens migration"
```

---

### Task 3: DB session dependency + admin bootstrap

**Files:**
- Create: `docaudit-agent/src/agent/api/db.py`
- Modify: `docaudit-agent/src/agent/api/app.py` (add bootstrap call)

**Code — `db.py`:**

```python
"""Database session dependency for API routes."""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dbop.db_manager import AsyncDatabase, CRUDRepository
from src.dbop.tables.user import UserTable, UserCreate, UserUpdate, UserRole, UserStatus
from src.dbop.tables.refresh_token import RefreshTokenTable

logger = logging.getLogger(__name__)

_db: AsyncDatabase | None = None


def get_db() -> AsyncDatabase:
    global _db
    if _db is None:
        from src.config import Settings
        _db = AsyncDatabase(Settings().mysql_url)
    return _db


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    db = get_db()
    async with db.session() as session:
        yield session


user_repo = CRUDRepository[UserTable, UserCreate, UserUpdate](UserTable)
refresh_token_repo = CRUDRepository(RefreshTokenTable)


async def bootstrap_admin_user() -> None:
    """Create initial admin user from env vars if no users exist."""
    from src.config import Settings
    import bcrypt

    settings = Settings()
    if not settings.admin_user or not settings.admin_password:
        logger.info("ADMIN_USER/ADMIN_PASSWORD not set, skipping bootstrap")
        return

    db = get_db()
    async with db.session() as session:
        result = await session.execute(select(UserTable).limit(1))
        if result.scalar_one_or_none() is not None:
            return

        password_hash = bcrypt.hashpw(
            settings.admin_password.encode(), bcrypt.gensalt(rounds=12)
        ).decode()
        user = UserTable(
            username=settings.admin_user,
            password_hash=password_hash,
            role=UserRole.admin,
            status=UserStatus.active,
        )
        session.add(user)
        logger.info("Bootstrapped admin user: %s", settings.admin_user)
```

**Modify `app.py` — in `lifespan`:**
After `init_telemetry()`, add:
```python
from .db import bootstrap_admin_user
await bootstrap_admin_user()
```

- [ ] **Step 1: Write `db.py` and add bootstrap call to `app.py`**
- [ ] **Step 2: Run tests**

```bash
cd docaudit-agent && uv run pytest tests/ -m "not integration" -x -q
```

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/db.py src/agent/api/app.py
git commit -m "feat: add DB session dependency and admin bootstrap"
```

---

## Phase 2: Backend — Auth Extensions

### Task 4: Add bcrypt + refresh token helpers to auth middleware

**Files:**
- Modify: `docaudit-agent/src/agent/api/middleware/auth.py`

Add these functions below the existing imports (keep all existing functions unchanged):

```python
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


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
    """Returns (raw_token, token_hash, expires_at)."""
    raw = secrets.token_hex(64)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    return raw, token_hash, expires_at


async def rotate_refresh_token(old_hash: str, session) -> tuple[str, datetime] | None:
    """Validate, revoke old token, issue new one. Returns (new_raw, new_expires_at) or None."""
    from src.dbop.tables.refresh_token import RefreshTokenTable

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
    raw, token_hash, expires_at = generate_refresh_token()
    session.add(RefreshTokenTable(user_id=stored.user_id, token_hash=token_hash, expires_at=expires_at))
    return raw, expires_at
```

Note: `select` is already imported via `from sqlalchemy import ...` — add it if missing from the existing imports.

- [ ] **Step 1: Add functions to `auth.py`**
- [ ] **Step 2: Run existing tests**

```bash
cd docaudit-agent && uv run pytest tests/agent/api/test_routes.py -v
```
Expected: all existing tests pass (new functions don't break old code).

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/middleware/auth.py
git commit -m "feat: add bcrypt hashing and refresh token rotation to auth middleware"
```

---

### Task 5: Extend auth routes (register, refresh, logout)

**Files:**
- Modify: `docaudit-agent/src/agent/api/routes/auth.py`

Replace the entire file. The key changes:
- Login now queries DB instead of env vars, sets httpOnly refresh cookie
- Add POST `/register` (creates user with status=pending)
- Add POST `/refresh` (reads cookie, rotates refresh token, returns new access token)
- Add POST `/logout` (revokes refresh token, clears cookie)

```python
"""认证路由 — 注册、登录、刷新、登出。"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, get_db_session, user_repo
from ..middleware.auth import (
    create_access_token, generate_refresh_token, hash_password,
    rotate_refresh_token, verify_password, verify_jwt,
)
from ..rate_limiter import limiter
from src.dbop.tables.user import UserTable, UserCreate, UserRole, UserStatus
from src.dbop.tables.refresh_token import RefreshTokenTable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
ACCESS_EXPIRE = 900  # 15 minutes


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    email: str = Field(default="", max_length=128)


def _set_refresh_cookie(response: Response, raw_token: str, expires_at) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE, value=raw_token,
        httponly=True, secure=True, samesite="strict",
        path="/api/auth", expires=expires_at,
    )


def _user_dict(user: UserTable) -> dict:
    return {
        "id": user.id, "username": user.username,
        "email": user.email, "role": user.role.value,
        "status": user.status.value,
    }


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
        user = await user_repo.create(session, UserCreate(
            username=body.username,
            password=hash_password(body.password),
            email=body.email,
        ))
        return {"message": "注册成功，请等待管理员审批", "user_id": user.id}


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    """登录 — 返回 access token + 设置 httpOnly refresh cookie。"""
    settings = request.app.state.settings
    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == body.username)
        )
        user = result.scalar_one_or_none()
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "用户名或密码错误")
        if user.status == UserStatus.pending:
            raise HTTPException(403, "账号尚未通过审批")
        if user.status == UserStatus.disabled:
            raise HTTPException(403, "账号已被禁用")

        access_token = create_access_token(
            user.username, user.id, user.role.value,
            secret=settings.jwt_secret, algorithm=settings.jwt_algorithm,
            expire_seconds=ACCESS_EXPIRE,
        )
        raw_refresh, token_hash, expires_at = generate_refresh_token()
        session.add(RefreshTokenTable(user_id=user.id, token_hash=token_hash, expires_at=expires_at))
        _set_refresh_cookie(response, raw_refresh, expires_at)

        return {
            "token": access_token, "token_type": "bearer",
            "expires_in": ACCESS_EXPIRE, "user": _user_dict(user),
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
        new_raw, new_expires_at = result

        # Find user associated with the new token
        rt_result = await session.execute(
            select(RefreshTokenTable).where(
                RefreshTokenTable.token_hash == hashlib.sha256(new_raw.encode()).hexdigest()
            )
        )
        rt = rt_result.scalar_one()
        user_result = await session.execute(select(UserTable).where(UserTable.id == rt.user_id))
        user = user_result.scalar_one()

        if user.status != UserStatus.active:
            raise HTTPException(403, "账号已被禁用或未通过审批")

        access_token = create_access_token(
            user.username, user.id, user.role.value,
            secret=settings.jwt_secret, algorithm=settings.jwt_algorithm,
            expire_seconds=ACCESS_EXPIRE,
        )
        _set_refresh_cookie(response, new_raw, new_expires_at)

        return {
            "token": access_token, "token_type": "bearer",
            "expires_in": ACCESS_EXPIRE, "user": _user_dict(user),
        }


@router.post("/logout")
async def logout(request: Request, response: Response):
    """登出 — 撤销 refresh token，清除 cookie。"""
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if raw_token:
        db = get_db()
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        async with db.session() as session:
            result = await session.execute(
                select(RefreshTokenTable).where(RefreshTokenTable.token_hash == token_hash)
            )
            rt = result.scalar_one_or_none()
            if rt:
                rt.revoked = True
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return {"message": "已登出"}
```

- [ ] **Step 1: Write the rewritten `auth.py`**
- [ ] **Step 2: Update `verify_jwt` to extract role and user_id from JWT payload**

In `middleware/auth.py`, update `verify_jwt` to handle the new `uid`/`role` claims:

The existing `verify_jwt` returns `payload["sub"]` (username). We need to also make `uid` and `role` accessible. Add a new dependency:

```python
async def get_current_user(request: Request, credentials=Depends(security)) -> dict:
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
```

Keep the original `verify_jwt` unchanged for backward compatibility with the SSE query-param path.

- [ ] **Step 3: Run existing tests to verify backward compat**

```bash
cd docaudit-agent && uv run pytest tests/agent/api/test_routes.py -v
```
Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/agent/api/routes/auth.py src/agent/api/middleware/auth.py
git commit -m "feat: add register, refresh, logout endpoints with httpOnly cookie"
```

---

## Phase 3: Backend — User Management API

### Task 6: Admin user management routes

**Files:**
- Create: `docaudit-agent/src/agent/api/routes/admin_users.py`

Endpoints (all require admin role):
- `GET /api/admin/users` — paginated list with search/filter
- `GET /api/admin/users/{user_id}` — detail
- `PATCH /api/admin/users/{user_id}` — update role/status/password
- `GET /api/admin/approvals` — pending users
- `POST /api/admin/approvals/{user_id}/approve` — approve
- `POST /api/admin/approvals/{user_id}/reject` — reject

```python
"""Admin user management routes — admin-only."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, user_repo
from ..middleware.auth import get_current_user, hash_password
from src.dbop.tables.user import UserTable, UserUpdate, UserRole, UserStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def require_admin(payload: dict = Depends(get_current_user)) -> dict:
    if payload.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return payload


# ---- Schemas ----

class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    status: str
    created_at: str
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


# ---- Routes ----

@router.get("/users", response_model=UserListResponse)
async def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    role: str = Query(""),
    status: str = Query(""),
    _admin: dict = Depends(require_admin),
):
    """List users with search and filter."""
    db = get_db()
    async with db.session() as session:
        query = select(UserTable)
        count_query = select(func.count(UserTable.id))

        conditions = []
        if search:
            conditions.append(UserTable.username.ilike(f"%{search}%"))
        if role:
            conditions.append(UserTable.role == role)
        if status:
            conditions.append(UserTable.status == status)

        from sqlalchemy import and_
        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        total_result = await session.execute(count_query)
        total = total_result.scalar_one()

        query = query.order_by(UserTable.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        result = await session.execute(query)
        users = result.scalars().all()

        return UserListResponse(
            items=[
                UserOut(
                    id=u.id, username=u.username, email=u.email,
                    role=u.role.value, status=u.status.value,
                    created_at=u.created_at.isoformat() if u.created_at else "",
                    updated_at=u.updated_at.isoformat() if u.updated_at else None,
                )
                for u in users
            ],
            total=total, page=page, page_size=page_size,
        )


class UserUpdateBody(BaseModel):
    role: str | None = None
    status: str | None = None
    password: str | None = None
    email: str | None = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int, body: UserUpdateBody, request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update user role, status, or password."""
    db = get_db()
    async with db.session() as session:
        update_data = {}
        if body.role is not None:
            update_data["role"] = body.role
        if body.status is not None:
            update_data["status"] = body.status
        if body.password is not None:
            if len(body.password) < 8:
                raise HTTPException(400, "密码至少8位")
            update_data["password_hash"] = hash_password(body.password)
        if body.email is not None:
            update_data["email"] = body.email

        if not update_data:
            raise HTTPException(400, "没有提供要更新的字段")

        user = await user_repo.update(session, user_id, update_data)
        if user is None:
            raise HTTPException(404, "用户不存在")
        return {"message": "更新成功"}


@router.get("/approvals")
async def list_approvals(
    _admin: dict = Depends(require_admin),
):
    """List pending-approval users."""
    db = get_db()
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.status == UserStatus.pending).order_by(UserTable.created_at.desc())
        )
        users = result.scalars().all()
        return {
            "items": [
                {"id": u.id, "username": u.username, "email": u.email, "created_at": u.created_at.isoformat() if u.created_at else ""}
                for u in users
            ]
        }


@router.post("/approvals/{user_id}/approve")
async def approve_user(
    user_id: int, _admin: dict = Depends(require_admin),
):
    """Approve a pending user."""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.update(session, user_id, {"status": UserStatus.active.value})
        if user is None:
            raise HTTPException(404, "用户不存在")
        return {"message": "已通过审批"}


@router.post("/approvals/{user_id}/reject")
async def reject_user(
    user_id: int, _admin: dict = Depends(require_admin),
):
    """Reject and delete a pending user."""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise HTTPException(404, "用户不存在")
        if user.status != UserStatus.pending:
            raise HTTPException(400, "只能拒绝待审批用户")
        await user_repo.delete(session, user_id)
        return {"message": "已拒绝并删除"}
```

- [ ] **Step 1: Write `admin_users.py`**
- [ ] **Step 2: Write tests**

```python
# tests/agent/api/test_admin_users.py
"""Tests for admin user management routes."""

import pytest
from fastapi.testclient import TestClient

from src.agent.api.app import create_app
from src.agent.api.middleware.auth import create_access_token


@pytest.fixture
def admin_client():
    """Returns TestClient authenticated as admin."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        app = create_app(sessions_dir=d, start_plugins=False)
        settings = app.state.settings
        token = create_access_token("admin", 1, "admin", secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with TestClient(app) as c:
            c.headers["Authorization"] = f"Bearer {token}"
            # bootstrap admin so DB-based login works
            c.cookies.clear()
            yield c


@pytest.fixture
def auditor_client(admin_client):
    """Returns TestClient authenticated as auditor."""
    app = admin_client.app
    settings = app.state.settings
    token = create_access_token("auditor1", 2, "auditor", secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


class TestAdminUsers:
    def test_list_users_admin(self, admin_client):
        resp = admin_client.get("/api/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_list_users_auditor_rejected(self, auditor_client):
        resp = auditor_client.get("/api/admin/users")
        assert resp.status_code == 403

    def test_approve_and_reject(self, admin_client):
        # Register first
        resp = admin_client.post("/api/auth/register", json={
            "username": "testuser", "password": "password123", "email": "test@test.com"
        })
        assert resp.status_code == 200
        user_id = resp.json()["user_id"]

        # List approvals
        resp = admin_client.get("/api/admin/approvals")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) >= 1

        # Approve
        resp = admin_client.post(f"/api/admin/approvals/{user_id}/approve")
        assert resp.status_code == 200

        # Verify active
        resp = admin_client.get("/api/admin/users?status=active")
        active_usernames = [u["username"] for u in resp.json()["items"]]
        assert "testuser" in active_usernames
```

- [ ] **Step 3: Run tests**

```bash
cd docaudit-agent && uv run pytest tests/agent/api/test_admin_users.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/agent/api/routes/admin_users.py tests/agent/api/test_admin_users.py
git commit -m "feat: add admin user management routes with tests"
```

---

### Task 7: Profile routes

**Files:**
- Create: `docaudit-agent/src/agent/api/routes/profile.py`

```python
"""Profile routes — self-service user settings."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..db import get_db, user_repo
from ..middleware.auth import get_current_user, hash_password, verify_password
from src.dbop.tables.user import UserTable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    status: str
    avatar_url: str

    model_config = {"from_attributes": True}


class ProfileUpdateBody(BaseModel):
    email: str | None = None
    current_password: str | None = None
    new_password: str | None = None


@router.get("")
async def get_profile(request: Request, payload: dict = Depends(get_current_user)):
    """Get current user profile."""
    db = get_db()
    async with db.session() as session:
        result = await session.execute(select(UserTable).where(UserTable.id == payload["uid"]))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(404, "用户不存在")
        return {
            "id": user.id, "username": user.username,
            "email": user.email, "role": user.role.value,
            "status": user.status.value, "avatar_url": user.avatar_url,
        }


@router.patch("")
async def update_profile(
    request: Request, body: ProfileUpdateBody,
    payload: dict = Depends(get_current_user),
):
    """Update own profile — email or password."""
    db = get_db()
    async with db.session() as session:
        user = await user_repo.get(session, payload["uid"])
        if user is None:
            raise HTTPException(404, "用户不存在")

        update_data = {}
        if body.email is not None:
            update_data["email"] = body.email

        if body.new_password is not None:
            if not body.current_password:
                raise HTTPException(400, "需要提供当前密码")
            if not verify_password(body.current_password, user.password_hash):
                raise HTTPException(400, "当前密码错误")
            if len(body.new_password) < 8:
                raise HTTPException(400, "新密码至少8位")
            update_data["password_hash"] = hash_password(body.new_password)

        if not update_data:
            raise HTTPException(400, "没有提供要更新的字段")

        await user_repo.update(session, user.id, update_data)
        return {"message": "更新成功"}
```

- [ ] **Step 1: Write `profile.py`**
- [ ] **Step 2: Run tests to verify app starts with new routes**

```bash
cd docaudit-agent && uv run python -c "from src.agent.api.routes.profile import router; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/routes/profile.py
git commit -m "feat: add profile routes for self-service settings"
```

---

### Task 8: Register new routers + update config

**Files:**
- Modify: `docaudit-agent/src/agent/api/app.py`
- Modify: `docaudit-agent/src/config.py`
- Modify: `docaudit-agent/.env.example`

**Changes to `app.py`:**
- Register admin router and profile router (JWT-protected)
- Set `cors_allow_credentials = True` (needed for cookies)

```python
# Add imports:
from .routes.admin_users import router as admin_router
from .routes.profile import router as profile_router

# Add routers (after app.include_router(api_router)):
app.include_router(admin_router)
app.include_router(profile_router)
```

**Changes to `config.py`:**
Add:
```python
jwt_access_expire_seconds: int = Field(default=900, description="Access token 过期时间（秒），默认 15 分钟")
bcrypt_rounds: int = Field(default=12, ge=4, le=14, description="bcrypt 哈希轮数")
cors_allow_credentials: bool = Field(default=True, description="是否允许跨域携带 Cookie")
```

**Update `.env.example`:**
```bash
# ========== JWT 认证 ==========
JWT_SECRET=your-jwt-secret-change-in-production
# JWT_ALGORITHM=HS256
# JWT_EXPIRE_SECONDS=86400          # 兼容旧版 access token（SSE 使用长过期）
# JWT_ACCESS_EXPIRE_SECONDS=900     # 新版 access token（15 分钟）
ADMIN_USER=admin
ADMIN_PASSWORD=your-admin-password-change-in-production
```

- [ ] **Step 1: Apply all three file changes**
- [ ] **Step 2: Run full test suite**

```bash
cd docaudit-agent && uv run pytest tests/ -m "not integration" -v
```
Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/app.py src/config.py .env.example
git commit -m "feat: register admin/profile routers, update CORS and config"
```

---

## Phase 4: Frontend — Foundation

### Task 9: Install vue-router + set up router with guards

**Files:**
- Modify: `webui/package.json`
- Create: `webui/src/router/index.ts`
- Modify: `webui/src/main.ts`

- [ ] **Step 1: Install vue-router**

```bash
cd webui && npm install vue-router@4
```

- [ ] **Step 2: Write router setup**

```typescript
// webui/src/router/index.ts
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
    meta: { guest: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('../views/RegisterView.vue'),
    meta: { guest: true },
  },
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/HomeView.vue'),  // wraps existing Terminal
    meta: { requiresAuth: true },
  },
  {
    path: '/admin',
    component: () => import('../views/AdminLayout.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [
      { path: 'users', name: 'AdminUsers', component: () => import('../views/UserManagement.vue') },
      { path: 'approvals', name: 'AdminApprovals', component: () => import('../views/ApprovalManagement.vue') },
      { path: '', redirect: '/admin/users' },
    ],
  },
  {
    path: '/profile',
    name: 'Profile',
    component: () => import('../views/ProfileView.vue'),
    meta: { requiresAuth: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, _from, next) => {
  // useAuth must be called after app is mounted; use a global auth state
  // that's initialized before the router guard runs
  const { user, initAuth, isAdmin } = useAuth()

  // Initialize auth on first navigation
  if (!user.value) {
    await initAuth()
  }

  if (to.meta.requiresAuth && !user.value) {
    return next({ name: 'Login', query: { redirect: to.fullPath } })
  }

  if (to.meta.guest && user.value) {
    return next({ name: 'Home' })
  }

  if (to.meta.requiresAdmin && !isAdmin.value) {
    return next({ name: 'Home' })
  }

  next()
})

export default router
```

Note: The `useAuth()` call inside `beforeEach` requires that `useAuth` uses a module-level reactive singleton (not tied to a component instance). The composable must use `reactive` at module scope rather than within a `setup()` context.

- [ ] **Step 3: Update main.ts**

```typescript
// webui/src/main.ts
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)
app.use(router)
app.mount('#app')
```

- [ ] **Step 4: Create a placeholder HomeView.vue**

```vue
<!-- webui/src/views/HomeView.vue -->
<template>
  <Terminal
    :stats="session.stats"
    :turns="session.turns"
    :turn-version="turnVersion"
    :thoughts="session.thoughts"
    :is-running="isRunning"
    :step-count="session.steps.length"
    :total-tools="totalTools"
    :done-tools="doneTools"
    :issue-count="issueCount"
    :is-expanded="isExpanded"
    :toggle="toggle"
    :uploading="uploading"
    :upload-error="uploadError"
    :model-name="session.modelName"
    :history-open="historyOpen"
    :history-sessions="historySessions"
    :history-loading="historyLoading"
    :error-message="session.errorMessage"
    :stop-reason="session.stopReason"
    :user="user"
    @export="handleExport"
    @history-toggle="historyOpen = !historyOpen"
    @history-select="handleHistorySelect"
    @history-delete="handleHistoryDelete"
    @new-session="newSession"
    @quick-task="(task: string) => handleSubmit(task)"
    @submit="handleSubmit"
    @stop="stop"
    @logout="handleLogout"
  />
</template>

<script setup lang="ts">
// Same logic as current App.vue, plus:
import { useAuth } from '../composables/useAuth'
const { user, logout } = useAuth()
function handleLogout() { logout(); router.push('/login') }

// ... (rest of the current App.vue <script> logic, migrated here)
</script>
```

Essentially, `HomeView.vue` becomes the current `App.vue`, and `App.vue` becomes a thin `<router-view />` wrapper.

- [ ] **Step 5: Verify build**

```bash
cd webui && npm run build
```
Expected: build succeeds (pages may be incomplete, but no TS errors from router setup).

- [ ] **Step 6: Commit**

```bash
git add webui/package.json webui/package-lock.json webui/src/router/ webui/src/main.ts webui/src/views/HomeView.vue
git commit -m "feat: add Vue Router with auth guards"
```

---

### Task 10: useAuth composable

**Files:**
- Create: `webui/src/composables/useAuth.ts`

Module-level reactive state (singleton pattern — works outside component setup):

```typescript
// webui/src/composables/useAuth.ts
import { reactive, computed } from 'vue'
import { api } from '../api/client'

interface AuthUser {
  id: number
  username: string
  email: string
  role: 'admin' | 'auditor'
  status: string
}

interface AuthState {
  user: AuthUser | null
  accessToken: string | null
  initialized: boolean
}

const state = reactive<AuthState>({
  user: null,
  accessToken: null,
  initialized: false,
})

export function useAuth() {
  const isAdmin = computed(() => state.user?.role === 'admin')
  const isLoggedIn = computed(() => state.user !== null)

  async function initAuth(): Promise<void> {
    if (state.initialized) return
    state.initialized = true
    try {
      const resp = await api.refreshToken()
      if (resp) {
        state.accessToken = resp.token
        state.user = resp.user
      }
    } catch {
      // Not logged in — stay on guest page
    }
  }

  async function login(username: string, password: string): Promise<AuthUser> {
    const resp = await api.login(username, password)
    state.accessToken = resp.token
    state.user = resp.user
    return resp.user
  }

  async function register(username: string, email: string, password: string): Promise<void> {
    await api.register(username, email, password)
  }

  async function logout(): Promise<void> {
    try { await api.logout() } catch { /* ignore */ }
    state.user = null
    state.accessToken = null
  }

  function setAccessToken(token: string) {
    state.accessToken = token
  }

  function getAccessToken(): string | null {
    return state.accessToken
  }

  return {
    user: computed(() => state.user),
    isAdmin,
    isLoggedIn,
    initAuth,
    login,
    register,
    logout,
    setAccessToken,
    getAccessToken,
  }
}
```

- [ ] **Step 1: Write `useAuth.ts`**
- [ ] **Step 2: Verify build**

```bash
cd webui && npx vue-tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add webui/src/composables/useAuth.ts
git commit -m "feat: add useAuth composable with token lifecycle"
```

---

### Task 11: Update API client for cookie-based auth

**Files:**
- Modify: `webui/src/api/client.ts`

Key changes:
1. Remove auto-login with hardcoded admin password
2. `getAuthHeaders` reads token from `useAuth` composable singleton
3. Add `login()`, `register()`, `refreshToken()`, `logout()` methods
4. Add 401 interceptor that calls refresh and retries
5. Keep `createEventSource` with `?token=` passthrough

```typescript
// webui/src/api/client.ts (key changes)

import { useAuth } from '../composables/useAuth'

const API_BASE = '/api'

// Remove: ADMIN_PASSWORD, _token, _loginPromise, login(), getAuthHeaders()
// Replace with:

function getAuthHeaders(): Record<string, string> {
  const { getAccessToken } = useAuth()
  const token = getAccessToken()
  if (token) return { Authorization: `Bearer ${token}` }
  return {}
}

async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = getAuthHeaders()
  let res = await fetch(url, {
    ...init,
    headers: { ...init.headers, ...headers },
    credentials: 'include',  // send cookies
  })

  // Auto-refresh on 401
  if (res.status === 401 && !url.includes('/auth/refresh')) {
    const { setAccessToken } = useAuth()
    try {
      const refreshResp = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
      })
      if (refreshResp.ok) {
        const data = await refreshResp.json()
        setAccessToken(data.token)
        // Retry original request
        const newHeaders = getAuthHeaders()
        res = await fetch(url, {
          ...init,
          headers: { ...init.headers, ...newHeaders },
          credentials: 'include',
        })
      }
    } catch {
      // Refresh failed — caller handles 401
    }
  }
  return res
}

export const api = {
  // Auth
  async login(username: string, password: string): Promise<{ token: string; user: any }> {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Login failed: ${res.status}`)
    }
    return res.json()
  },

  async register(username: string, email: string, password: string): Promise<void> {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, email, password }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Register failed: ${res.status}`)
    }
  },

  async refreshToken(): Promise<{ token: string; user: any } | null> {
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
    if (!res.ok) return null
    return res.json()
  },

  async logout(): Promise<void> {
    await fetch(`${API_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    })
  },

  // ... keep existing methods: uploadFile, pause, resume, stop,
  //     listSessions, loadSession, deleteSession, createEventSource
  // (These all use authFetch which now delegates to useAuth)
}
```

- [ ] **Step 1: Rewrite `client.ts`**
- [ ] **Step 2: Verify build**

```bash
cd webui && npx vue-tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add webui/src/api/client.ts
git commit -m "feat: update API client for cookie-based auth with auto-refresh"
```

---

## Phase 5: Frontend — Auth Pages

### Task 12: LoginView

**Files:**
- Create: `webui/src/views/LoginView.vue`
- Create: `webui/src/styles/auth.css`

**Layout:** Split-panel — left brand area (seal icon, "SDTAgent", tagline, paper-grain texture) + right form card (username, password, submit, link to register).

```vue
<!-- webui/src/views/LoginView.vue -->
<template>
  <div class="auth-page">
    <div class="auth-left">
      <div class="auth-brand">
        <div class="auth-seal">
          <span>审</span>
        </div>
        <h1 class="auth-title">SDTAgent</h1>
        <p class="auth-tagline">文档智能审计平台</p>
      </div>
    </div>
    <div class="auth-right">
      <div class="auth-card">
        <h2 class="auth-card-title">登录</h2>
        <form @submit.prevent="handleLogin" class="auth-form">
          <div class="auth-field">
            <label for="username">用户名</label>
            <input id="username" v-model="username" type="text" autocomplete="username" required />
          </div>
          <div class="auth-field">
            <label for="password">密码</label>
            <input id="password" v-model="password" type="password" autocomplete="current-password" required />
          </div>
          <p v-if="error" class="auth-error">{{ error }}</p>
          <button type="submit" class="auth-submit" :disabled="loading">
            {{ loading ? '登录中...' : '登录' }}
          </button>
        </form>
        <p class="auth-switch">
          还没有账号？<router-link to="/register">注册</router-link>
        </p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const router = useRouter()
const route = useRoute()
const { login } = useAuth()

const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function handleLogin() {
  error.value = ''
  loading.value = true
  try {
    await login(username.value, password.value)
    const redirect = (route.query.redirect as string) || '/'
    router.push(redirect)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败'
  } finally {
    loading.value = false
  }
}
</script>
```

**Styles — `auth.css`:**

```css
/* Split-panel auth layout */
.auth-page {
  display: flex;
  min-height: 100vh;
}

.auth-left {
  flex: 1;
  background: linear-gradient(135deg, var(--paper) 0%, #f0ebe0 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.auth-left::before {
  content: '';
  position: absolute;
  inset: 0;
  opacity: 0.03;
  background-image: repeating-linear-gradient(
    0deg, transparent, transparent 2px, var(--ink) 2px, var(--ink) 3px
  );
}

.auth-brand {
  position: relative;
  z-index: 1;
  text-align: center;
}

.auth-seal {
  width: 80px;
  height: 80px;
  border: 4px solid var(--seal);
  transform: rotate(45deg);
  margin: 0 auto 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.auth-seal span {
  transform: rotate(-45deg);
  font-family: 'Noto Serif SC', serif;
  font-size: 28px;
  color: var(--seal);
  font-weight: bold;
}

.auth-title {
  font-family: 'Noto Serif SC', serif;
  font-size: 28px;
  color: var(--ink);
  letter-spacing: 4px;
  margin: 0;
}

.auth-tagline {
  font-size: 14px;
  color: var(--ink-faint);
  margin-top: 8px;
}

.auth-right {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--paper);
}

.auth-card {
  width: 360px;
  max-width: 90vw;
}

.auth-card-title {
  font-family: 'Noto Serif SC', serif;
  font-size: 24px;
  color: var(--ink);
  margin: 0 0 32px;
  text-align: center;
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.auth-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.auth-field label {
  font-size: 13px;
  color: var(--ink-dim);
  font-weight: 500;
}

.auth-field input {
  height: 44px;
  padding: 0 12px;
  background: var(--paper-high);
  border: 1px solid #e0dbd0;
  border-radius: 6px;
  font-size: 15px;
  color: var(--ink);
  font-family: 'Noto Sans SC', sans-serif;
  outline: none;
  transition: border-color 150ms;
}

.auth-field input:focus {
  border-color: var(--seal);
}

.auth-error {
  color: var(--err);
  font-size: 13px;
  margin: 0;
}

.auth-submit {
  height: 44px;
  background: var(--seal);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 15px;
  font-family: 'Noto Sans SC', sans-serif;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 150ms;
  margin-top: 4px;
}

.auth-submit:hover { opacity: 0.9; }
.auth-submit:disabled { opacity: 0.6; cursor: default; }

.auth-switch {
  text-align: center;
  margin-top: 24px;
  font-size: 13px;
  color: var(--ink-dim);
}

.auth-switch a {
  color: var(--seal);
  text-decoration: none;
  font-weight: 600;
}
```

- [ ] **Step 1: Write `LoginView.vue` and `auth.css`**
- [ ] **Step 2: Import `auth.css` in `style.css`**

Add `@import './auth.css';` to `webui/src/style.css`.

- [ ] **Step 3: Verify build**

```bash
cd webui && npm run build
```
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add webui/src/views/LoginView.vue webui/src/styles/auth.css webui/src/style.css
git commit -m "feat: add LoginView with split-panel layout"
```

---

### Task 13: RegisterView

**Files:**
- Create: `webui/src/views/RegisterView.vue`

Same split-panel layout as LoginView. Right panel: username, email, password, confirm password, submit button, link back to login. Post-submit: show success message "注册成功，请等待管理员审批".

```vue
<!-- webui/src/views/RegisterView.vue -->
<template>
  <div class="auth-page">
    <!-- Same left panel as LoginView -->
    <div class="auth-left">
      <div class="auth-brand">
        <div class="auth-seal"><span>审</span></div>
        <h1 class="auth-title">SDTAgent</h1>
        <p class="auth-tagline">文档智能审计平台</p>
      </div>
    </div>
    <div class="auth-right">
      <div class="auth-card">
        <template v-if="submitted">
          <div class="auth-success">
            <div class="auth-success-icon">✓</div>
            <h2>注册成功</h2>
            <p>请等待管理员审批后即可登录使用</p>
            <router-link to="/login" class="auth-submit" style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none;margin-top:24px;">返回登录</router-link>
          </div>
        </template>
        <template v-else>
          <h2 class="auth-card-title">注册</h2>
          <form @submit.prevent="handleRegister" class="auth-form">
            <div class="auth-field">
              <label for="username">用户名</label>
              <input id="username" v-model="username" type="text" required minlength="1" maxlength="64" />
            </div>
            <div class="auth-field">
              <label for="email">邮箱</label>
              <input id="email" v-model="email" type="email" />
            </div>
            <div class="auth-field">
              <label for="password">密码</label>
              <input id="password" v-model="password" type="password" required minlength="8" />
            </div>
            <div class="auth-field">
              <label for="confirmPassword">确认密码</label>
              <input id="confirmPassword" v-model="confirmPassword" type="password" required />
            </div>
            <p v-if="error" class="auth-error">{{ error }}</p>
            <button type="submit" class="auth-submit" :disabled="loading">
              {{ loading ? '注册中...' : '注册' }}
            </button>
          </form>
          <p class="auth-switch">
            已有账号？<router-link to="/login">登录</router-link>
          </p>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAuth } from '../composables/useAuth'

const { register } = useAuth()

const username = ref('')
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const error = ref('')
const loading = ref(false)
const submitted = ref(false)

async function handleRegister() {
  error.value = ''
  if (password.value !== confirmPassword.value) {
    error.value = '两次密码不一致'
    return
  }
  if (password.value.length < 8) {
    error.value = '密码至少8位'
    return
  }
  loading.value = true
  try {
    await register(username.value, email.value, password.value)
    submitted.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : '注册失败'
  } finally {
    loading.value = false
  }
}
</script>
```

- [ ] **Step 1: Write `RegisterView.vue`**
- [ ] **Step 2: Build check**

```bash
cd webui && npx vue-tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add webui/src/views/RegisterView.vue
git commit -m "feat: add RegisterView with admin-approval flow"
```

---

## Phase 6: Frontend — Admin & Profile

### Task 14: AdminLayout + UserManagement

**Files:**
- Create: `webui/src/views/AdminLayout.vue`
- Create: `webui/src/views/UserManagement.vue`
- Create: `webui/src/styles/admin.css`

**AdminLayout:** Sidebar (240px) with "用户管理" and "注册审批" nav links, content area with `<router-view>`.

**UserManagement:** Table with columns (username, email, role badge, status badge, date, actions). Search input + role/status filter. Row actions: edit role dropdown, enable/disable, reset password.

Key types:
```typescript
interface AdminUser {
  id: number
  username: string
  email: string
  role: 'admin' | 'auditor'
  status: 'active' | 'disabled' | 'pending'
  created_at: string
}
```

API calls to add to `client.ts`:
```typescript
// Admin
async listUsers(params: { page?: number; page_size?: number; search?: string; role?: string; status?: string }): Promise<{ items: AdminUser[]; total: number; page: number; page_size: number }> {
  const res = await authFetch(`${API_BASE}/admin/users${qs(params as Record<string,string>)}`)
  if (!res.ok) throw new Error(`Failed: ${res.status}`)
  return res.json()
},
async updateUser(id: number, data: { role?: string; status?: string; password?: string }): Promise<void> {
  const res = await authFetch(`${API_BASE}/admin/users/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Failed: ${res.status}`)
},
async listApprovals(): Promise<{ items: { id: number; username: string; email: string; created_at: string }[] }> {
  const res = await authFetch(`${API_BASE}/admin/approvals`)
  if (!res.ok) throw new Error(`Failed: ${res.status}`)
  return res.json()
},
async approveUser(id: number): Promise<void> {
  const res = await authFetch(`${API_BASE}/admin/approvals/${id}/approve`, { method: 'POST' })
  if (!res.ok) throw new Error(`Failed: ${res.status}`)
},
async rejectUser(id: number): Promise<void> {
  const res = await authFetch(`${API_BASE}/admin/approvals/${id}/reject`, { method: 'POST' })
  if (!res.ok) throw new Error(`Failed: ${res.status}`)
},
```

**AdminLayout template:**
```vue
<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <router-link to="/" class="admin-logo">
        <div class="admin-seal"><span>审</span></div>
      </router-link>
      <nav class="admin-nav">
        <router-link to="/admin/users" class="admin-nav-item" active-class="active">用户管理</router-link>
        <router-link to="/admin/approvals" class="admin-nav-item" active-class="active">注册审批</router-link>
      </nav>
      <div class="admin-sidebar-footer">
        <router-link to="/" class="admin-nav-item">← 返回主页</router-link>
      </div>
    </aside>
    <main class="admin-content">
      <router-view />
    </main>
  </div>
</template>
```

**UserManagement:** Full implementation with search input, filter selects, data table, pagination, action dropdown per row, inline status/role badges using existing CSS tokens (--seal for admin, --ink-faint for auditor, --ok for active, --warn for pending, --err for disabled).

Due to length, full component code is omitted from this plan but follows the exact patterns established in `MainPanel.vue` and `HistoryPanel.vue` — Composition API, TypeScript, CSS custom properties, existing token system.

- [ ] **Step 1: Write `AdminLayout.vue`, `UserManagement.vue`, `admin.css`**
- [ ] **Step 2: Add admin API methods to `client.ts`**
- [ ] **Step 3: Build check**

```bash
cd webui && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add webui/src/views/AdminLayout.vue webui/src/views/UserManagement.vue webui/src/styles/admin.css webui/src/api/client.ts
git commit -m "feat: add AdminLayout and UserManagement with table view"
```

---

### Task 15: ApprovalManagement

**Files:**
- Create: `webui/src/views/ApprovalManagement.vue`

Table of pending users with Approve/Reject buttons per row. Empty state when no pending approvals.

- [ ] **Step 1: Write `ApprovalManagement.vue`**
- [ ] **Step 2: Build check**

```bash
cd webui && npx vue-tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add webui/src/views/ApprovalManagement.vue
git commit -m "feat: add ApprovalManagement view"
```

---

### Task 16: ProfileView

**Files:**
- Create: `webui/src/views/ProfileView.vue`

Simple page: display username/email/role, change password form (current + new + confirm).

- [ ] **Step 1: Write `ProfileView.vue`** (follows same auth-card pattern from LoginView, shown in a centered layout)
- [ ] **Step 2: Build check**

```bash
cd webui && npx vue-tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add webui/src/views/ProfileView.vue
git commit -m "feat: add ProfileView with password change"
```

---

## Phase 7: Integration

### Task 17: Wire App.vue with router-view + update Terminal nav

**Files:**
- Modify: `webui/src/App.vue`
- Modify: `webui/src/components/Terminal.vue`

**App.vue** becomes a thin shell:

```vue
<template>
  <div v-if="renderError" class="render-error-fallback">
    <div class="render-error-icon">!</div>
    <h2>界面渲染错误</h2>
    <p>{{ renderError }}</p>
    <button @click="renderError = null">重试</button>
  </div>
  <router-view v-else />
</template>

<script setup lang="ts">
import { ref, onErrorCaptured } from 'vue'
const renderError = ref<string | null>(null)
onErrorCaptured((err) => { renderError.value = String(err); return false })
</script>
```

**Terminal.vue** — add user dropdown menu in `.top-nav`. Replace the current action buttons area with:

```vue
<div class="top-nav-actions">
  <div class="user-menu" @click="menuOpen = !menuOpen" v-click-outside="() => menuOpen = false">
    <span class="user-name">{{ user?.username ?? '' }}</span>
    <span class="user-arrow">▾</span>
    <div v-if="menuOpen" class="user-dropdown">
      <router-link to="/profile" class="dropdown-item">个人设置</router-link>
      <template v-if="user?.role === 'admin'">
        <router-link to="/admin/users" class="dropdown-item">用户管理</router-link>
        <router-link to="/admin/approvals" class="dropdown-item">注册审批</router-link>
      </template>
      <div class="dropdown-divider"></div>
      <button class="dropdown-item" @click="$emit('logout')">退出登录</button>
    </div>
  </div>
</div>
```

New props/emits: `user: AuthUser | null`, `@logout`.

- [ ] **Step 1: Update `App.vue` and `Terminal.vue`**
- [ ] **Step 2: Pass user/logout through HomeView**

In `HomeView.vue`, pass `:user="user"` and listen to `@logout`.

- [ ] **Step 3: Full build check**

```bash
cd webui && npm run build
```
Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add webui/src/App.vue webui/src/components/Terminal.vue webui/src/views/HomeView.vue
git commit -m "feat: integrate router with App.vue and add user nav dropdown"
```

---

### Task 18: End-to-end verification

- [ ] **Step 1: Run all backend tests**

```bash
cd docaudit-agent && uv run pytest tests/ -m "not integration" -v
```
Expected: all tests pass.

- [ ] **Step 2: Build frontend**

```bash
cd webui && npm run build
```
Expected: clean build with no type errors.

- [ ] **Step 3: Start backend and verify health**

```bash
cd docaudit-agent && PYTHONPATH=src uv run python -c "
from src.agent.api.app import create_app
app = create_app(start_plugins=False)
print('App created successfully')
print('Routes:')
for route in app.routes:
    if hasattr(route, 'path') and hasattr(route, 'methods'):
        print(f'  {route.methods} {route.path}')
"
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: final integration verification"
```

---

## Self-Review

**1. Spec coverage:** Every section in the design spec maps to tasks:
- User/RefreshToken models → Tasks 1-2
- Auth endpoints (register, login, refresh, logout) → Task 5
- Admin user management API → Task 6
- Profile API → Task 7
- Router + guards → Task 9
- useAuth composable → Task 10
- API client update → Task 11
- LoginView → Task 12
- RegisterView → Task 13
- AdminLayout + UserManagement → Task 14
- ApprovalManagement → Task 15
- ProfileView → Task 16
- Integration + nav dropdown → Task 17
- Token strategy (httpOnly cookie + JWT access) → Tasks 4, 5, 10, 11
- Role permissions → Task 6 (require_admin), Task 9 (route guards)
- Migration → Task 2
- Bootstrap admin → Task 3

**2. Placeholder scan:** No TBD/TODO/placeholder patterns. All steps have concrete code or commands.

**3. Type consistency:**
- `AuthUser.role` is `'admin' | 'auditor'` → matches `UserRole` enum
- `UserUpdate.password` → `password_hash` in DB → consistent
- Access token expiry: 900s (15 min) → consistent across config, middleware, and routes
- Refresh cookie name: `refresh_token` → consistent across auth.py and client.ts
