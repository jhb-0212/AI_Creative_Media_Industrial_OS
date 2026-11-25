"""修复：artifact_manifests 的 status CHECK 未包含 ABANDONED。

问题
----
``a7b8c9d0e1f2``（PR-G-2/PR-G-3）给 ArtifactManifest 增加 ``ABANDONED`` 终态与
``PREPARED → ABANDONED`` 边，ORM 的 ``status_check(...)`` 已按枚举生成三值，
但数据库里的 ``ck_artifact_manifests_status`` 仍是 ``e1f2a3b4c5d6`` 建表时的
两值（PREPARED / STORED_VERIFIED）。

为什么闸门没抓到
----------------
``alembic check`` 走 autogenerate，而 SQLAlchemy 明确不比较 CHECK 约束：
「ORM 三值、库里两值」这种漂移对它完全不可见。因此这条缺口不是「闸门判错了」，
而是「闸门没有这项能力」。

后果
----
任何 ``PREPARED → ABANDONED`` 的落库都会撞 CHECK 违例。两条生产路径都会失败：

- ``services/production_planning/service/generation_service.py`` 的 T1 放弃路径；
- ``workers/generation/executor.py`` 恢复时判定「对象确定不存在」的放弃路径。

失败后意图永远停在 PREPARED：每次恢复都会重新尝试、可能重复计费，而「已放弃」
这个事实无法落纸。这是 PR-G 恢复链条的核心故障路径，不是边角情况。

为什么新增迁移而不是改 ``a7b8c9d0e1f2``
--------------------------------------
那条迁移已在本地与 CI 环境执行过。改历史迁移会让「已升级的库」与「新升级的库」
出现同一 revision、不同结构的分歧——而这正是迁移机制要防的事。

降级
----
降级会把允许集合收回到两值。若库里存在 ABANDONED 行，PG 会让约束校验失败；
这里先显式检查并抛带计数的错误，避免「约束违例」这种看不出原因的失败。不静默
改数据：ABANDONED 行是「该意图已被放弃」的事实，不能在降级时被抹成 PREPARED。

Revision ID: e7f8a9b0c1d2
Revises: c5d6e7f8a9b0
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.status_constraint import status_check_sql

revision = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "production_planning"
_TABLE = "artifact_manifests"
_CONSTRAINT = "ck_artifact_manifests_status"

# 与 ORM ``ArtifactManifestStatus`` 全集一致；status_check_sql 按字母序渲染，
# 因此这里的书写顺序不影响生成结果（也与建表迁移同口径）。
_WITH_ABANDONED = ("ABANDONED", "PREPARED", "STORED_VERIFIED")
_WITHOUT_ABANDONED = ("PREPARED", "STORED_VERIFIED")


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, status_check_sql(_WITH_ABANDONED), schema=_SCHEMA
    )


def downgrade() -> None:
    abandoned = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM {_SCHEMA}.{_TABLE} WHERE status = 'ABANDONED'"
            )
        )
        .scalar_one()
    )
    if abandoned:
        raise RuntimeError(
            f"无法降级 {revision}：{_SCHEMA}.{_TABLE} 存在 {abandoned} 条 ABANDONED 记录。"
            "降级会把允许集合收回两值；这些行是「意图已被放弃」的既有事实，"
            "不能为了让迁移通过而静默改写。请先确认这些记录的处置方式"
            "（保留则不要降级此迁移），再重试。"
        )
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, status_check_sql(_WITHOUT_ABANDONED), schema=_SCHEMA
    )
