"""drop residual document user_id and page save_path columns

docmodels 重构的 DB 层收尾：删除两列已无人读写的残余列。

- documents.user_id：NOT NULL 默认 ""，模型重构后 Document 已无该字段，
  save_doc 不再写入真值（去重键已简化为 doc_id 单键），load_doc 不再读取。
- pages.save_path：NOT NULL 默认 "默认路径"，模型重构后 Page 已无该字段，
  save_page 不再写入（沿用列默认值），无任何读取方。

downgrade 以 server_default 回填恢复两列（user_id → ''，save_path →
'默认路径'），与原 ORM default 一致。

Revision ID: 43e68af4536c
Revises: b71c4f0e9d23
Create Date: 2026-08-06 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43e68af4536c"
down_revision: Union[str, Sequence[str], None] = "b71c4f0e9d23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # documents：删除 user_id（模型已无该字段，无读写方）
    op.drop_column("documents", "user_id")

    # pages：删除 save_path（模型已无该字段，无读写方）
    op.drop_column("pages", "save_path")


def downgrade() -> None:
    """Downgrade schema."""
    # pages：恢复 save_path（存量行以 server_default '默认路径' 回填，
    # 与原 ORM default 一致）
    op.add_column(
        "pages",
        sa.Column(
            "save_path",
            sa.String(length=512),
            nullable=False,
            server_default="默认路径",
            comment="保存路径",
        ),
    )

    # documents：恢复 user_id（存量行以 server_default '' 回填，
    # 与原 ORM default "" 一致）
    op.add_column(
        "documents",
        sa.Column(
            "user_id",
            sa.String(length=512),
            nullable=False,
            server_default="",
            comment="文件所属用户的id值",
        ),
    )
