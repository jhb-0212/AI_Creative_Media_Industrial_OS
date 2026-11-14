"""清理 outbox 中的测试残留事件（P3-3）

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-11-14

`tests/unit/test_unit_of_work.py` 曾直接 INSERT `aggregate_type='UoWTest'`
事件并提交，测试结束后没有清理（app_role 无 DELETE，这个不变量本身是
对的，但测试也没用迁移角色清理），导致真实库留下 9 条 UOW_TEST 事件。

本迁移只删"测试构造物"：aggregate_type='UoWTest' 且 event_type='UOW_TEST'。
生产事件的 aggregate_type 全部是领域类型（Project / Episode / AssetVersion
/ ScriptRevision），不会命中；条件写得越窄，误删风险越小。

测试侧同步改为用迁移角色自清理（见 test_unit_of_work.py），否则删完还会再长。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM outbox.outbox_events "
            "WHERE aggregate_type = 'UoWTest' AND event_type = 'UOW_TEST'"
        )
    )


def downgrade() -> None:
    # 数据清理不可逆（删掉的测试残留没有重建价值）。
    pass
