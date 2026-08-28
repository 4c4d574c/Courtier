"""Settings key-value ORM models (DB-backed configuration storage).

One row per Settings field that has been explicitly set through the
admin settings API.  Fields absent here fall back to the env/default
chain (ConfigService snapshot composition).  Secret values are stored
as ``{"__enc__": "<fernet token>"}`` encrypted with
``COURTIER_SETTINGS_KEY`` — never plaintext.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

#: BIGINT autoincrement on MySQL, plain INTEGER on sqlite (the dialect
#: only auto-increments INTEGER primary keys — keeps store tests portable).
_BigIntId = BigInteger().with_variant(Integer, "sqlite")


class SettingsTable(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(
        String(190), primary_key=True, comment="Settings 字段名（python 属性名）"
    )
    value: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        comment="字段值；is_secret=true 时为 {'__enc__': <fernet 密文>}",
    )
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="值是否加密存储"
    )
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, default="advanced", index=True, comment="设置分组"
    )
    updated_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="最后修改人（用户名）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="最后修改时间"
    )


class SettingsChangeTable(Base):
    """Audit trail: who changed which key and when.  Values are recorded
    as sha256 hashes only — secret or not, plaintext never lands here.
    MAX(id) doubles as the global settings version (ConfigService)."""

    __tablename__ = "settings_changes"

    id: Mapped[int] = mapped_column(
        _BigIntId, primary_key=True, autoincrement=True, comment="变更ID"
    )
    key_name: Mapped[str] = mapped_column(
        String(190), nullable=False, index=True, comment="Settings 字段名"
    )
    old_hash: Mapped[str] = mapped_column(String(64), nullable=True, comment="旧值 sha256")
    new_hash: Mapped[str] = mapped_column(String(64), nullable=True, comment="新值 sha256")
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="操作人")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True, comment="变更时间"
    )
