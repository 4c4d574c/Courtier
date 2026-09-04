"""add deletion_requests and deletion_log tables

账号注销：deletion_requests 承载自助注销流（密码验证的申请 → 管理员
审批 → 管线执行；user_id 不建 FK——执行时用户行即被删除，行保留作
记录）；deletion_log 为清除回执（append-only，各类清除条数 + 外部
清理失败步骤）。

Revision ID: b9c2f6d8a1e3
Revises: f7d3a9b2c4e1
Create Date: 2026-09-04 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c2f6d8a1e3"
down_revision: Union[str, Sequence[str], None] = "f7d3a9b2c4e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="申请ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="目标用户ID（无FK：用户行随执行删除）"),
        sa.Column("status", sa.String(16), nullable=False, comment='状态："pending" | "approved" | "rejected" | "cancelled" | "executed"'),
        sa.Column("requested_by", sa.String(16), nullable=False, comment='发起方："self" | "admin"'),
        sa.Column("decided_by", sa.String(64), nullable=False, comment="决定人（管理员用户名）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="申请时间"),
        sa.Column("decided_at", sa.DateTime(), nullable=True, comment="决定时间"),
    )
    op.create_index("ix_deletion_requests_user_id", "deletion_requests", ["user_id"])
    op.create_index("ix_deletion_requests_status", "deletion_requests", ["status"])

    op.create_table(
        "deletion_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="回执ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="被注销用户ID"),
        sa.Column("trigger", sa.String(16), nullable=False, comment='触发方式："self_approved" | "admin"'),
        sa.Column("executed_by", sa.String(64), nullable=False, comment="执行人（管理员用户名）"),
        sa.Column("counts", sa.JSON(), nullable=False, comment="各类数据清除条数"),
        sa.Column("failures", sa.JSON(), nullable=False, comment="外部清理失败步骤"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="执行时间"),
    )
    op.create_index("ix_deletion_log_user_id", "deletion_log", ["user_id"])
    op.create_index("ix_deletion_log_created_at", "deletion_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_deletion_log_created_at", table_name="deletion_log")
    op.drop_index("ix_deletion_log_user_id", table_name="deletion_log")
    op.drop_table("deletion_log")
    op.drop_index("ix_deletion_requests_status", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_user_id", table_name="deletion_requests")
    op.drop_table("deletion_requests")
