"""create project_planning.projects table

Revision ID: 8d8d59a91012
Revises: 
Create Date: 2026-11-13 12:46:03.813283

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d8d59a91012'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 先创建独立 schema（数据所有权边界）。
    op.execute("CREATE SCHEMA IF NOT EXISTS project_planning")
    op.create_table('projects',
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('entity_type', sa.String(length=64), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('entity_version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('owner_module', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('audit_ref', sa.String(length=128), nullable=True),
    sa.Column('project_name', sa.String(length=200), nullable=False),
    sa.Column('project_tier', sa.String(length=8), nullable=False),
    sa.PrimaryKeyConstraint('entity_id'),
    schema='project_planning'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('projects', schema='project_planning')
