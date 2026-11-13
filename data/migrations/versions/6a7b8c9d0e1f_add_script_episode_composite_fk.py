"""add composite foreign key script_revisions -> episodes

根因（审查报告 P0-2 数据模型无约束）：
- script_revisions 冗余存储 (project_id, episode_ref) 自然键，但无任何
  数据库外键约束，归属正确性完全依赖 Service 层查询校验（R2 违反）。
- 决策 2：通过复合外键在数据库层锁死「跨项目乱挂载」——任何写入的
  (project_id, episode_ref) 必须精确匹配一个真实存在的 Episode。

改动：
1. episodes 的 ux_episode_ref_per_project 从「唯一索引」提升为「唯一约束」
   （复合外键的引用目标，PostgreSQL 外键须引用唯一约束/主键）。
2. script_revisions 新增复合外键 (project_id, episode_ref) ->
   episodes(project_id, episode_ref)。

Revision ID: 6a7b8c9d0e1f
Revises: 0fcaf4db4573
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a7b8c9d0e1f"
down_revision: str | Sequence[str] | None = "0fcaf4db4573"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "project_planning"


def upgrade() -> None:
    """提升唯一索引为唯一约束 + 清理悬空数据 + 新增复合外键。"""
    # 0. 清理悬空数据：删除引用不存在 Episode 的 script_revisions。
    #    根因：Episode 实体（迁移 0fcaf4db4573）加入前，script_revisions 已
    #    存在且无归属验证，产生了引用不存在 Episode 的历史脏数据。复合外键
    #    要求 (project_id, episode_ref) 必须匹配真实 Episode，故先行清理。
    #    这些记录业务上非法（归属悬空），删除是唯一正确处置。
    op.execute(
        f"""
        DELETE FROM {SCHEMA}.script_revisions sr
        WHERE NOT EXISTS (
            SELECT 1 FROM {SCHEMA}.episodes e
            WHERE e.project_id = sr.project_id AND e.episode_ref = sr.episode_ref
        )
        """
    )

    # 1. 先删旧唯一索引（原 0fcaf4db4573 创建），腾出约束名空间。
    op.drop_index("ux_episode_ref_per_project", table_name="episodes", schema=SCHEMA)

    # 2. 建唯一约束（复合外键的引用目标）。
    op.create_unique_constraint(
        "ux_episode_ref_per_project",
        "episodes",
        ["project_id", "episode_ref"],
        schema=SCHEMA,
    )

    # 3. 加复合外键：script_revisions(project_id, episode_ref) -> episodes。
    op.create_foreign_key(
        "fk_script_revisions_episode",
        "script_revisions",
        "episodes",
        ["project_id", "episode_ref"],
        ["project_id", "episode_ref"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    """回滚：删复合外键 + 唯一约束还原为唯一索引。"""
    op.drop_constraint(
        "fk_script_revisions_episode",
        "script_revisions",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "ux_episode_ref_per_project",
        "episodes",
        schema=SCHEMA,
        type_="unique",
    )
    op.create_index(
        "ux_episode_ref_per_project",
        "episodes",
        ["project_id", "episode_ref"],
        unique=True,
        schema=SCHEMA,
    )
