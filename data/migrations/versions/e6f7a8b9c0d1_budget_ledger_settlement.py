"""BudgetLedger 释放/冲销：reservation_ref + 受控 transaction_kind（Phase 2 §2）

这一版为什么必须先解决「指回哪一笔」
------------------------------------
释放（RELEASE）与冲销（REFUND）都作用在**某一条已存在的 RESERVE 流水**上。如果
流水里没有指向它的引用，那么：

- 无法回答「这笔释放对应哪次预留」，账龄与超支分析直接失效；
- 无法在库层拒绝「同一笔预留被释放两次」——服务层「先查一下」在并发下必然漏。

所以这条迁移加两样东西，一个都不能省：
1. ``reservation_ref``：结算流水指回被处理的 RESERVE 流水（RESERVE 自身为空）；
2. ``uq_budget_quota_ledger_reservation_kind`` **部分**唯一索引：同一
   (reservation_ref, transaction_kind) 只能出现一次，且只对非空 reservation_ref
   生效——普通唯一约束会把所有 RESERVE 的空串判成重复，等于建表即失败。

第三个：``transaction_kind`` 受控
--------------------------------
此前 ``transaction_kind`` 是裸 VARCHAR，写 ``"RELEASED"`` 或 ``"release"`` 都能落库，
余额口径按字符串匹配时会静默漏算。CHECK 从
``services.budget_quota.domain.enums`` 派生（``RESERVE / RELEASE / REFUND``），迁移不
写字面量状态值：加新种类必须改枚举，改枚举就会同时改到这里。

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.budget_quota.domain.enums import transaction_kind_values

revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "budget_quota"
_TABLE = "budget_ledger"
_RESERVATION_INDEX = "uq_budget_quota_ledger_reservation_kind"
_KIND_CHECK = "ck_budget_ledger_transaction_kind"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("reservation_ref", sa.String(128), nullable=False, server_default=""),
        schema=_SCHEMA,
    )

    # 部分唯一索引：只约束「真的指回了某笔预留」的行。
    op.create_index(
        _RESERVATION_INDEX,
        _TABLE,
        ["reservation_ref", "transaction_kind"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("reservation_ref <> ''"),
    )

    op.create_check_constraint(
        _KIND_CHECK,
        _TABLE,
        "transaction_kind IN ("
        + ", ".join(f"'{value}'" for value in transaction_kind_values())
        + ")",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(_KIND_CHECK, _TABLE, schema=_SCHEMA, type_="check")
    op.drop_index(_RESERVATION_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_column(_TABLE, "reservation_ref", schema=_SCHEMA)


__all__ = ["down_revision", "downgrade", "revision", "upgrade"]
