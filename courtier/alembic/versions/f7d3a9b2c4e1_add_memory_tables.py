"""add memory and memory_changes tables

记忆分层与 DB 化：memory 每行一个记忆条目（layer 分全局共享层/用户层，
domain 分通用/领域包，owner_id 0=全局层哨兵、domain "common"=通用哨兵，
四键列全 NOT NULL 使唯一约束在 MySQL 下真实去重）；memory_changes 为
审计流水（内容只记 sha256 哈希，明文不落库，删除不可恢复）。

Revision ID: f7d3a9b2c4e1
Revises: e5c90b1a7d42
Create Date: 2026-09-04 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7d3a9b2c4e1"
down_revision: Union[str, Sequence[str], None] = "e5c90b1a7d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="条目ID"),
        sa.Column("layer", sa.String(16), nullable=False, comment='层："global" | "user"'),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="所属用户ID；0=全局层"),
        sa.Column("domain", sa.String(64), nullable=False, comment='领域包归类；"common"=通用（保留字）'),
        sa.Column("title", sa.String(190), nullable=False, comment="条目标题（寻址键）"),
        sa.Column("content", sa.Text(), nullable=False, comment="条目内容"),
        sa.Column("created_by", sa.String(64), nullable=False, comment="创建人（用户名）"),
        sa.Column("updated_by", sa.String(64), nullable=False, comment="最后修改人（用户名）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="最后修改时间"),
        sa.UniqueConstraint("layer", "owner_id", "domain", "title", name="uq_memory_address"),
    )
    op.create_index("ix_memory_layer", "memory", ["layer"])
    op.create_index("ix_memory_owner_id", "memory", ["owner_id"])

    op.create_table(
        "memory_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="变更ID"),
        sa.Column("entry_id", sa.BigInteger(), nullable=False, comment="条目ID"),
        sa.Column("action", sa.String(16), nullable=False, comment='动作："create" | "update" | "delete"'),
        sa.Column("layer", sa.String(16), nullable=False, comment="层"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="所属用户ID；0=全局层"),
        sa.Column("domain", sa.String(64), nullable=False, comment="领域包归类"),
        sa.Column("title", sa.String(190), nullable=False, comment="条目标题"),
        sa.Column("old_hash", sa.String(64), nullable=True, comment="旧内容 sha256"),
        sa.Column("new_hash", sa.String(64), nullable=True, comment="新内容 sha256"),
        sa.Column("actor", sa.String(64), nullable=False, comment="操作人（用户名或 agent:<用户名>）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="变更时间"),
    )
    op.create_index("ix_memory_changes_entry_id", "memory_changes", ["entry_id"])
    op.create_index("ix_memory_changes_owner_id", "memory_changes", ["owner_id"])
    op.create_index("ix_memory_changes_created_at", "memory_changes", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_changes_created_at", table_name="memory_changes")
    op.drop_index("ix_memory_changes_owner_id", table_name="memory_changes")
    op.drop_index("ix_memory_changes_entry_id", table_name="memory_changes")
    op.drop_table("memory_changes")
    op.drop_index("ix_memory_owner_id", table_name="memory")
    op.drop_index("ix_memory_layer", table_name="memory")
    op.drop_table("memory")
