"""align document tables with refactored docmodels

docmodels 重构（MetaData → LineElement，删除 raw/exist/block_no 等字段，
间距/缩进全 Optional 化，新增 schema_version/warnings/alignment）后的
DB 层对齐：

- documents：doc_id 注释 md5 → sha256；新增 schema_version（String(8)，
  非空，server_default '1.0' 回填存量行）与 warnings（JSON，nullable，
  存量行为 NULL）。
- pages：删除 raw LONGBLOB 列。
- paragraphs：删除 block_no 列；space_before/space_after/line_spacing/
  first_indent/left_indent/right_indent 6 列改为 nullable（None 表示
  未提取，与 0.0 区分）；新增 alignment（String(16)，nullable——应用层
  总是写入实际值，NULL 仅标记本列新增前的存量行）。
- elements：删除 exist 列。

enum 无变化：paragraphs.outline_level 的 7 值 enum 与模型 Literal 已一致；
alignment 按任务约定使用 String(16) 而非 enum，无需 DDL 处理。

Revision ID: b71c4f0e9d23
Revises: a3f7c21d9e84
Create Date: 2026-08-06 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b71c4f0e9d23"
down_revision: Union[str, Sequence[str], None] = "a3f7c21d9e84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# paragraphs 中由 NOT NULL 放宽为 nullable 的间距/缩进列（含注释，保持元数据不变）
_SPACING_COLUMNS = [
    ("space_before", "段前间距"),
    ("space_after", "段后间距"),
    ("line_spacing", "行距"),
    ("first_indent", "首行缩进"),
    ("left_indent", "文本之前缩进(pt)"),
    ("right_indent", "文本之后缩进(pt)"),
]


def upgrade() -> None:
    """Upgrade schema."""
    # documents：新列 + doc_id 注释修正
    op.add_column(
        "documents",
        sa.Column(
            "schema_version",
            sa.String(length=8),
            nullable=False,
            server_default="1.0",
            comment="模型结构版本号",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "warnings",
            sa.JSON(),
            nullable=True,
            comment="解析过程告警信息列表（JSON数组）",
        ),
    )
    op.alter_column(
        "documents",
        "doc_id",
        existing_type=sa.String(length=512),
        nullable=False,
        comment="文件内容的sha256值",
        existing_comment="文件的内容的md5值",
    )

    # pages：删除 raw
    op.drop_column("pages", "raw")

    # paragraphs：删除 block_no、间距/缩进放宽 nullable、新增 alignment
    op.drop_column("paragraphs", "block_no")
    for col_name, col_comment in _SPACING_COLUMNS:
        op.alter_column(
            "paragraphs",
            col_name,
            existing_type=mysql.FLOAT(),
            nullable=True,
            existing_comment=col_comment,
        )
    op.add_column(
        "paragraphs",
        sa.Column(
            "alignment",
            sa.String(length=16),
            nullable=True,
            comment="对齐方式",
        ),
    )

    # elements：删除 exist
    op.drop_column("elements", "exist")


def downgrade() -> None:
    """Downgrade schema."""
    # elements：恢复 exist（存量行回填 1，与原 default=True 一致）
    op.add_column(
        "elements",
        sa.Column(
            "exist",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否存在",
        ),
    )

    # paragraphs：恢复 block_no、间距/缩进改回 NOT NULL（NULL 先回填 0.0）、删 alignment
    op.drop_column("paragraphs", "alignment")
    op.add_column(
        "paragraphs",
        sa.Column(
            "block_no",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="块序号",
        ),
    )
    for col_name, col_comment in _SPACING_COLUMNS:
        op.execute(f"UPDATE paragraphs SET {col_name} = 0.0 WHERE {col_name} IS NULL")
        op.alter_column(
            "paragraphs",
            col_name,
            existing_type=mysql.FLOAT(),
            nullable=False,
            existing_comment=col_comment,
        )

    # pages：恢复 raw
    op.add_column(
        "pages",
        sa.Column("raw", mysql.LONGBLOB(), nullable=True, comment="原始内容"),
    )

    # documents：还原 doc_id 注释并删除新增列
    op.alter_column(
        "documents",
        "doc_id",
        existing_type=sa.String(length=512),
        nullable=False,
        comment="文件的内容的md5值",
        existing_comment="文件内容的sha256值",
    )
    op.drop_column("documents", "warnings")
    op.drop_column("documents", "schema_version")
