"""Database session dependency for API routes."""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.db.db_manager import AsyncDatabase, CRUDRepository
from courtier.db.tables.refresh_token import RefreshTokenTable
from courtier.db.tables.user import UserCreate, UserTable, UserUpdate

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


