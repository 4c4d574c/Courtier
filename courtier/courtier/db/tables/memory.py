"""Memory ORM models — layered, DB-backed memory (global + per-user).

One row per memory entry.  The ``layer`` dimension splits the global
shared layer (domain knowledge, team norms — written by admins only)
from per-user layers (preferences, personal context — strictly scoped
to their owner).  Both layers carry the ``domain`` dimension inherited
from the retired file-based layout (``common/`` + ``<domain>/``):
``"common"`` marks domain-agnostic entries, a loaded domain-package
name marks domain entries.

No NULL defaults: the global layer is ``owner_id = 0`` (real user ids
start at 1), generic entries are ``domain = "common"`` (a reserved
domain-package name).  All four key columns being NOT NULL makes the
unique constraint real on MySQL — a NULL in any indexed column would
silently disable dedup.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

#: BIGINT autoincrement on MySQL, plain INTEGER on sqlite (same portability
#: trick as SettingsTable).
_BigIntId = BigInteger().with_variant(Integer, "sqlite")

#: layer values.
LAYER_GLOBAL = "global"
LAYER_USER = "user"

#: domain sentinel for domain-agnostic entries; reserved — a domain
#: package may not take this name (enforced by memory write validation).
DOMAIN_COMMON = "common"

#: owner_id sentinel for the global shared layer.
OWNER_GLOBAL = 0


class MemoryTable(Base):
    __tablename__ = "memory"
    __table_args__ = (
        UniqueConstraint("layer", "owner_id", "domain", "title", name="uq_memory_address"),
    )

    id: Mapped[int] = mapped_column(
        _BigIntId, primary_key=True, autoincrement=True, comment="条目ID"
    )
    layer: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True, comment='层："global" | "user"'
    )
    owner_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=OWNER_GLOBAL, index=True, comment="所属用户ID；0=全局层"
    )
    domain: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DOMAIN_COMMON,
        comment='领域包归类；"common"=通用（保留字）',
    )
    title: Mapped[str] = mapped_column(String(190), nullable=False, comment="条目标题（寻址键）")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="条目内容")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="创建人（用户名）")
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="最后修改人（用户名）")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="最后修改时间"
    )


class MemoryChangeTable(Base):
    """Audit trail: who created/updated/deleted which entry and when.

    Append-only; content is recorded as sha256 hashes only (same policy
    as ``settings_changes``) — deletions are not recoverable.  A clear
    operation is recorded as one ``delete`` row per removed entry.
    """

    __tablename__ = "memory_changes"

    id: Mapped[int] = mapped_column(
        _BigIntId, primary_key=True, autoincrement=True, comment="变更ID"
    )
    entry_id: Mapped[int] = mapped_column(_BigIntId, nullable=False, index=True, comment="条目ID")
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment='动作："create" | "update" | "delete"'
    )
    layer: Mapped[str] = mapped_column(String(16), nullable=False, comment="层")
    owner_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=OWNER_GLOBAL, index=True, comment="所属用户ID；0=全局层"
    )
    domain: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DOMAIN_COMMON, comment="领域包归类"
    )
    title: Mapped[str] = mapped_column(String(190), nullable=False, comment="条目标题")
    old_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="旧内容 sha256")
    new_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="新内容 sha256")
    actor: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="操作人（用户名或 agent:<用户名>）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True, comment="变更时间"
    )
