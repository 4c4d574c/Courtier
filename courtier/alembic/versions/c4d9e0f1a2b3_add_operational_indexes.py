"""add operational indexes: refresh_tokens.expires_at, resources visibility+created_at

审查（2026-09-05 夜间马拉松 R5/R6）补充：
- refresh_tokens.expires_at：登录路径顺带清扫过期行（无索引则全表扫描）；
- resources (visibility, created_at)：列表按可见性过滤 + created_at 排序，
  无索引时资源库到万级后列表接口退化。

Revision ID: c4d9e0f1a2b3
Revises: b9c2f6d8a1e3
Create Date: 2026-09-05 05:30:00.000000

"""

from typing import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d9e0f1a2b3"
down_revision: str | None = "b9c2f6d8a1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_resources_visibility_created_at",
        "resources",
        ["visibility", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_resources_visibility_created_at", table_name="resources")
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
