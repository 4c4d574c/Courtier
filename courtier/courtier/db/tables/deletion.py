"""Account deletion ORM models — deletion requests and the cleanup receipt.

``deletion_requests`` carries the self-service flow (password-verified
request → admin approval → pipeline execution); rows are kept after
execution as an audit trail — ``user_id`` intentionally has no FK because
the user row is deleted when the pipeline runs.

``deletion_log`` is the append-only cleanup receipt: one row per executed
account deletion, with per-category counts and any best-effort external
cleanup failures.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

_BigIntId = BigInteger().with_variant(Integer, "sqlite")

#: deletion request statuses.
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_CANCELLED = "cancelled"
REQUEST_EXECUTED = "executed"


class DeletionRequestTable(Base):
    __tablename__ = "deletion_requests"

    id: Mapped[int] = mapped_column(
        _BigIntId, primary_key=True, autoincrement=True, comment="申请ID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="目标用户ID（无FK：用户行随执行删除）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=REQUEST_PENDING, index=True,
        comment='状态："pending" | "approved" | "rejected" | "cancelled" | "executed"',
    )
    requested_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="self", comment='发起方："self" | "admin"'
    )
    decided_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="决定人（管理员用户名）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="申请时间"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, comment="决定时间"
    )


class DeletionLogTable(Base):
    __tablename__ = "deletion_log"

    id: Mapped[int] = mapped_column(
        _BigIntId, primary_key=True, autoincrement=True, comment="回执ID"
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="被注销用户ID")
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, comment='触发方式："self_approved" | "admin"'
    )
    executed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="执行人（管理员用户名）")
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, comment="各类数据清除条数")
    failures: Mapped[dict] = mapped_column(JSON, nullable=False, comment="外部清理失败步骤")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True, comment="执行时间"
    )
