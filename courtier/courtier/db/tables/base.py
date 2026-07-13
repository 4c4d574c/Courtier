from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Shared default factory for timestamp columns."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """ORM 基类，提供通用的字典式访问方法。"""

    def keys(self) -> list[str]:
        return list(self.__table__.columns.keys())

    def __getitem__(self, key: str):
        return getattr(self, key)
