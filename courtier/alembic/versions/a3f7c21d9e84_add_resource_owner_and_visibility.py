"""add owner_id and visibility to resources for personal/public library split

Adds resources.owner_id (FK users.id, nullable — NULL marks pre-existing
public rows) and resources.visibility (personal/public, default public).
Existing rows keep public visibility, preserving current behavior.

Revision ID: a3f7c21d9e84
Revises: c8fd52a7316a
Create Date: 2026-07-26 16:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f7c21d9e84'
down_revision: Union[str, Sequence[str], None] = 'c8fd52a7316a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'resources',
        sa.Column(
            'owner_id',
            sa.Integer(),
            nullable=True,
            comment='上传者用户 ID（个人资源库归属；NULL 为存量公共数据）',
        ),
    )
    op.create_foreign_key(
        'fk_resources_owner_id_users',
        'resources',
        'users',
        ['owner_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.add_column(
        'resources',
        sa.Column(
            'visibility',
            sa.String(length=16),
            nullable=False,
            server_default='public',
            comment='可见性：personal（个人库）/ public（公共库）',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_resources_owner_id_users', 'resources', type_='foreignkey')
    op.drop_column('resources', 'visibility')
    op.drop_column('resources', 'owner_id')
