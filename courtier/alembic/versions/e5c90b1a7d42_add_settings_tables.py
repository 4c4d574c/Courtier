"""add settings and settings_changes tables

配置中心化 Phase 1：DB 设置存储。settings 每行一个显式设置过的
Settings 字段（值 JSON，secret 以 {'__enc__': <fernet>} 加密存储，
密钥为 COURTIER_SETTINGS_KEY）；settings_changes 为审计流水（值只记
sha256 哈希，明文绝不落库）。未出现在表中的字段回退 env/默认值链
（ConfigService 快照合成）。

Revision ID: e5c90b1a7d42
Revises: 43e68af4536c
Create Date: 2026-08-28 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c90b1a7d42"
down_revision: Union[str, Sequence[str], None] = "43e68af4536c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(190), primary_key=True, comment="Settings 字段名"),
        sa.Column("value", sa.JSON(), nullable=False, comment="字段值；secret 为加密密文"),
        sa.Column("is_secret", sa.Boolean(), nullable=False, comment="值是否加密存储"),
        sa.Column("category", sa.String(50), nullable=False, comment="设置分组"),
        sa.Column("updated_by", sa.String(64), nullable=False, comment="最后修改人（用户名）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="最后修改时间"),
    )
    op.create_index("ix_settings_category", "settings", ["category"])

    op.create_table(
        "settings_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="变更ID"),
        sa.Column("key_name", sa.String(190), nullable=False, comment="Settings 字段名"),
        sa.Column("old_hash", sa.String(64), nullable=True, comment="旧值 sha256"),
        sa.Column("new_hash", sa.String(64), nullable=True, comment="新值 sha256"),
        sa.Column("actor", sa.String(64), nullable=False, comment="操作人"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="变更时间"),
    )
    op.create_index("ix_settings_changes_key_name", "settings_changes", ["key_name"])
    op.create_index("ix_settings_changes_created_at", "settings_changes", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_settings_changes_created_at", table_name="settings_changes")
    op.drop_index("ix_settings_changes_key_name", table_name="settings_changes")
    op.drop_table("settings_changes")
    op.drop_index("ix_settings_category", table_name="settings")
    op.drop_table("settings")
