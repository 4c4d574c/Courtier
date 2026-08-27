"""Database session dependency for API routes."""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db.tables.refresh_token import RefreshTokenTable
from courtier.db.tables.user import UserCreate, UserRole, UserStatus, UserTable, UserUpdate

logger = logging.getLogger(__name__)

_db: AsyncDatabase | None = None


def get_db() -> AsyncDatabase:
    global _db
    if _db is None:
        from courtier.config import get_settings

        _db = AsyncDatabase(get_settings().mysql_url)
    return _db


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    db = get_db()
    async with db.session() as session:
        yield session


user_repo = CRUDRepository[UserTable, UserCreate, UserUpdate](UserTable)
refresh_token_repo: CRUDRepository[RefreshTokenTable, BaseModel, BaseModel] = CRUDRepository(
    RefreshTokenTable
)


async def bootstrap_admin_user() -> None:
    """Create initial admin user from env vars if no users exist.

    Gracefully skips if the database is unreachable — the admin user will
    need to be created manually or by a subsequent successful startup.
    """
    from courtier.agent.api.middleware.auth import hash_password
    from courtier.config import get_settings

    settings = get_settings()
    if not settings.admin_user or not settings.admin_password:
        logger.info("ADMIN_USER/ADMIN_PASSWORD not set, skipping bootstrap")
        return

    if not settings.mysql_url:
        logger.info("MYSQL_URL not set, skipping admin bootstrap")
        return

    try:
        db = get_db()
        async with db.session() as session:
            result = await session.execute(
                select(UserTable).where(UserTable.username == settings.admin_user)
            )
            if result.scalar_one_or_none() is not None:
                logger.info("Admin user %s already exists, skipping bootstrap", settings.admin_user)
                return

            user = UserTable(
                username=settings.admin_user,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.admin,
                status=UserStatus.active,
            )
            session.add(user)
            logger.info("Bootstrapped admin user: %s", settings.admin_user)
    except Exception as exc:
        exc_name = type(exc).__name__
        logger.warning(
            "Failed to bootstrap admin user (database may not be ready yet): %s: %s. "
            "The application will still start, but admin login requires a "
            "running database with the users table.",
            exc_name, exc,
        )
