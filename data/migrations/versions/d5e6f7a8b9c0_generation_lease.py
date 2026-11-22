"""GenerationTask 异步租约：RUNNING 状态 + 租约三列（Phase 2 §1）

为什么租约落在库里（而不是 Worker 进程里）
----------------------------------------
租约的全部作用是回答一个问题：「现在还有人在推进这个任务吗」。答案必须
对**所有**进程可见，否则 Worker 崩溃后没有任何一方能判断任务是「正在跑」还是
「被遗弃」。放进进程内存的租约在崩溃那一刻就随进程一起消失，任务永远停在
RUNNING——这是最典型的「看起来有病、查不出因病」。

三列的语义
----------
- ``lease_owner``：当前持有者标识（Worker 实例 id），空串表示无人持有；
- ``lease_expires_at``：租约到期时间；到期即可被别的 Worker 回收（RUNNING→REQUESTED）；
- ``attempt_count``：领取次数。状态机只能表达「当前在跑」，表达不了「第 9 次崩了」，
  而反复崩溃与首次失败的处理完全不同——所以次数必须单独记。

为什么同时改 CHECK 约束
------------------------
``ck_generation_tasks_status`` 是枚举的字面投影。只加列不改约束，写入 RUNNING 会
被数据库拒绝（应用层以为能写、库层报 500），这种「契约先行、约束滞后」的分叉
正是状态枚举必须集中生成的原因：``status_check`` 从同一份
``GenerationTaskStatus`` 派生成 CHECK，二者不可能不同步。

Revision ID: d5e6f7a8b9c0
Revises: a0b1c2d3e4f5
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.status_constraint import status_check_sql
from services.project_planning.domain.enums import GenerationTaskStatus

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "a0b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "production_planning"
_TABLE = "generation_tasks"
_STATUS_CHECK = f"ck_{_TABLE}_status"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("lease_owner", sa.String(128), nullable=False, server_default=""),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        schema=_SCHEMA,
    )

    # 约束换代：先删再建，CHECK 内容仍由枚举派生，迁移不写字面量状态值。
    # 必须取 `.value`：这个枚举是 (str, Enum)，str(成员) 会给出
    # "GenerationTaskStatus.RUNNING"，写进 CHECK 就等于允许任意字符串——
    # 一个「看起来有约束、实际什么都放行」的约束比没有约束更危险。
    op.drop_constraint(_STATUS_CHECK, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _STATUS_CHECK,
        _TABLE,
        status_check_sql([status.value for status in GenerationTaskStatus]),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(_STATUS_CHECK, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _STATUS_CHECK,
        _TABLE,
        status_check_sql(
            [
                GenerationTaskStatus.REQUESTED.value,
                GenerationTaskStatus.SUCCEEDED.value,
                GenerationTaskStatus.FAILED.value,
            ]
        ),
        schema=_SCHEMA,
    )
    op.drop_column(_TABLE, "attempt_count", schema=_SCHEMA)
    op.drop_column(_TABLE, "lease_expires_at", schema=_SCHEMA)
    op.drop_column(_TABLE, "lease_owner", schema=_SCHEMA)


__all__ = ["down_revision", "downgrade", "revision", "upgrade"]
