from __future__ import annotations

import logging
from typing import Any, Generic, Type, TypeVar
from contextlib import asynccontextmanager

from pydantic import BaseModel
from sqlalchemy import select, update, delete, insert, and_, func, Select, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
    AsyncEngine,
)

from .tables import Base

logger = logging.getLogger(__name__)

# 定义泛型
ModelType = TypeVar("ModelType", bound=Base)
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)


async def ensure_database_exists(db_url: str) -> None:
    """Create the target database if it does not exist.

    Supports ``mysql+asyncmy`` URLs. Silently skips for empty URLs or
    non-asyncmy drivers.

    Raises:
        Exception: If the database server is reachable but creation fails.
    """
    if not db_url:
        return

    url = make_url(db_url)
    if url.drivername not in ("mysql+asyncmy",):
        logger.debug("Skipping database auto-creation for driver: %s", url.drivername)
        return

    if not url.database:
        return

    # Connect to the server without specifying a database.
    server_url = url.set(database="")
    engine = create_async_engine(str(server_url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{url.database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        logger.info("Ensured database exists: %s", url.database)
    finally:
        await engine.dispose()


class AsyncDatabase:
    """数据库连接与会话管理器"""

    def __init__(self, db_url: str, echo: bool = False):
        self.engine: AsyncEngine = create_async_engine(db_url, echo=echo)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def ensure_database(self) -> None:
        """Ensure the configured database exists before connecting."""
        await ensure_database_exists(str(self.engine.url))

    async def create_all(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self, testing: bool = False):
        """Drop all tables managed by SQLAlchemy metadata.

        WARNING: This is destructive. Only allowed when testing=True.
        """
        if not testing:
            raise RuntimeError(
                "drop_all() is destructive. Pass testing=True to confirm."
            )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self):
        session: AsyncSession = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Session rollback due to error")
            raise
        finally:
            await session.close()


class CRUDRepository(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    """通用 CRUD 仓库，集成 Pydantic 和复杂查询。"""

    def __init__(self, model: Type[ModelType]):
        self.model = model

    @staticmethod
    def _to_dict(obj_in: BaseModel | dict[str, Any]) -> dict[str, Any]:
        """Convert a Pydantic model or raw dict into a plain dict."""
        if isinstance(obj_in, BaseModel):
            return obj_in.model_dump()
        return obj_in

    def _build_query(self, query: Select, filters: dict[str, Any]) -> Select:
        """复杂查询构建器，支持 __gt, __lt, __gte, __lte, __like, __ilike, __in, __neq, __is 操作符。"""
        conditions = []
        for attr, value in filters.items():
            if "__" in attr:
                field_name, op = attr.split("__", 1)
            else:
                field_name, op = attr, "eq"

            if not hasattr(self.model, field_name):
                continue

            column = getattr(self.model, field_name)

            if op == "eq":
                conditions.append(column == value)
            elif op == "neq":
                conditions.append(column != value)
            elif op == "gt":
                conditions.append(column > value)
            elif op == "lt":
                conditions.append(column < value)
            elif op == "gte":
                conditions.append(column >= value)
            elif op == "lte":
                conditions.append(column <= value)
            elif op == "like":
                conditions.append(column.like(value))
            elif op == "ilike":
                conditions.append(column.ilike(value))
            elif op == "in":
                conditions.append(column.in_(value))
            elif op == "is":
                conditions.append(column.is_(value))
            else:
                raise ValueError(f"Unsupported filter operator: {op!r}")

        if conditions:
            query = query.where(and_(*conditions))
        return query

    async def get(
        self,
        session: AsyncSession,
        id: Any,
        load_options: list | None = None,
    ) -> ModelType | None:
        query = select(self.model).where(self.model.id == id)
        if load_options:
            query = query.options(*load_options)
        result = await session.execute(query)
        return result.scalar_one_or_none()

    # Hard cap to prevent unbounded queries. Callers should use pagination;
    # override with an explicit limit if you genuinely need more rows.
    _DEFAULT_LIMIT = 1000
    _MAX_LIMIT = 10_000

    async def list(
        self,
        session: AsyncSession,
        skip: int = 0,
        limit: int | None = None,
        order_by: str | None = None,
        load_options: list | None = None,
        **filters,
    ) -> list[ModelType]:
        """支持复杂过滤和排序的列表查询。

        load_options: 可选的 SQLAlchemy loader options 列表
                      (e.g. [selectinload(Model.relation)]), 用于避免 N+1 查询。
        """
        # Clamp to safe defaults when no limit is specified.
        # limit=0 is treated as "return empty list" (callers wanting no rows).
        if limit is not None and limit <= 0:
            return []
        effective_limit = limit if limit is not None else self._DEFAULT_LIMIT
        effective_limit = min(effective_limit, self._MAX_LIMIT)

        query = select(self.model)
        if load_options:
            query = query.options(*load_options)
        query = self._build_query(query, filters)

        # 排序处理：支持逗号分隔的多字段排序
        # "-field" 降序，"field" 升序
        if order_by:
            for col in order_by.split(","):
                col = col.strip()
                if not col:
                    continue
                if col.startswith("-"):
                    field = getattr(self.model, col[1:], None)
                    if field is not None:
                        query = query.order_by(field.desc())
                else:
                    field = getattr(self.model, col, None)
                    if field is not None:
                        query = query.order_by(field.asc())

        query = query.offset(skip).limit(effective_limit)
        result = await session.execute(query)
        return result.scalars().all()

    async def create(
        self,
        session: AsyncSession,
        obj_in: CreateSchemaType | dict[str, Any],
    ) -> ModelType:
        """单条创建。"""
        obj_data = self._to_dict(obj_in)
        model_cols = {c.name for c in self.model.__table__.columns}
        obj_data = {k: v for k, v in obj_data.items() if k in model_cols}

        db_obj = self.model(**obj_data)
        session.add(db_obj)
        await session.flush()
        await session.refresh(db_obj)
        return db_obj

    async def update(
        self,
        session: AsyncSession,
        id: Any,
        obj_in: UpdateSchemaType | dict[str, Any],
    ) -> ModelType | None:
        """单条更新。"""
        if isinstance(obj_in, BaseModel):
            update_data = obj_in.model_dump(exclude_unset=True)
        else:
            update_data = obj_in

        stmt = (
            update(self.model)
            .where(self.model.id == id)
            .values(**update_data)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            return None
        return await self.get(session, id)

    async def delete(self, session: AsyncSession, id: Any) -> ModelType | None:
        """单条删除。"""
        ret_obj = await self.get(session, id)
        stmt = delete(self.model).where(self.model.id == id)
        result = await session.execute(stmt)
        if result.rowcount == 0:
            return None
        return ret_obj

    async def bulk_create(
        self,
        session: AsyncSession,
        objs_in: list[CreateSchemaType | dict[str, Any]],
    ) -> int:
        """批量插入（使用 Core Insert，性能极高）。

        Returns:
            插入的行数。
        """
        if not objs_in:
            return 0

        data_list = [self._to_dict(obj) for obj in objs_in]

        stmt = insert(self.model).values(data_list)
        result = await session.execute(stmt)
        return result.rowcount

    async def bulk_delete(
        self,
        session: AsyncSession,
        ids: list[Any | None] = None,
        **filters,
    ) -> int:
        """批量删除。

        :param ids: ID列表
        :param filters: 复杂过滤条件（如 age__lt=10）
        :return: 删除的行数
        """
        stmt = delete(self.model)

        if ids:
            stmt = stmt.where(self.model.id.in_(ids))

        if filters:
            stmt = self._build_query(stmt, filters)

        result = await session.execute(stmt)
        return result.rowcount

    async def count(
        self,
        session: AsyncSession,
        **filters,
    ) -> int:
        """统计符合条件的记录总数。"""
        query = select(func.count()).select_from(self.model)
        query = self._build_query(query, filters)
        result = await session.execute(query)
        return result.scalar_one()
