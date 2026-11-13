"""add script active version unique index

部分唯一索引：保证「同一项目 + 同一集数」下最多存在一个「非 SUPERSEDED」的
剧本版本（DRAFT/LOCKED 态）。这是并发取代的数据库兜底——当两个导入请求并发
对同一 (project_id, episode_ref) 插入新活跃版本时，后写入者撞唯一约束，触发
服务层串行化重试，从根上杜绝「重复取代 / 双活跃版本」竞态。

Revision ID: facd8d42d07f
Revises: e22d6fd3e8d3
Create Date: 2026-11-13 15:06:01.639904

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'facd8d42d07f'
down_revision: str | Sequence[str] | None = 'e22d6fd3e8d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 索引名（与 downgrade 对应，命名遵循 SQLAlchemy 约定）。
INDEX_NAME = "ux_script_active_version"
SCHEMA = "project_planning"
TABLE = "script_revisions"


def upgrade() -> None:
    """Upgrade schema: 新增部分唯一索引。"""
    op.create_index(
        INDEX_NAME,
        TABLE,
        ["project_id", "episode_ref"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'SUPERSEDED'"),
    )


def downgrade() -> None:
    """Downgrade schema: 移除部分唯一索引。"""
    op.drop_index(INDEX_NAME, table_name=TABLE, schema=SCHEMA)
