"""budget_quota 域 ORM 模型（自有 schema + 自有角色，DATA-1）。

为什么预算要独立成域
--------------------
账本是**钱的事实**：追加式不可变、幂等键唯一。它需要的权限形状（只有
SELECT/INSERT）与任何「状态推进型」业务表（SELECT/INSERT/UPDATE）都不同；
同角色同 schema 时只能给宽权限，或为了收窄而牺牲申请表的正常推进。物理分域后
``budget_quota_role`` 对 ``budget_ledger`` 只有 SELECT/INSERT——「改一笔已落库
的金额」在数据库层不可能，冲正只能新增一条流水。

迁移 ``a0b1c2d3e4f5`` 是本文件的 DDL 事实源。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin
from packages.shared.status_constraint import status_check
from services.budget_quota.domain.enums import transaction_kind_values
from services.project_planning.domain.enums import (
    NoLifecycleStatus,
    ShotBudgetRequestStatus,
)

SCHEMA_NAME = "budget_quota"


class ShotBudgetRequestModel(Base, EntityMixin):
    """shot_budget_requests：单镜预算申请（DRAFT→APPROVED/REJECTED）。"""

    __tablename__ = "shot_budget_requests"
    __table_args__ = (
        CheckConstraint("amount_limit >= 0", name="ck_shot_budget_requests_amount_nonneg"),
        Index("ix_budget_quota_shot_budget_requests_project_id", "project_id"),
        Index("ix_budget_quota_shot_budget_requests_tenant_id", "tenant_id"),
        status_check((s.value for s in ShotBudgetRequestStatus), table="shot_budget_requests"),
        {"schema": SCHEMA_NAME},
    )

    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    cost_category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approver: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    decision_reason: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")


class BudgetLedgerModel(Base, EntityMixin):
    """budget_ledger：追加式不可变流水（NO_LIFECYCLE，幂等键唯一）。"""

    __tablename__ = "budget_ledger"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_budget_ledger_idempotency_key"),
        # 同一笔预留只能被释放一次、冲销一次。用部分唯一索引而不是普通唯一约束：
        # RESERVE 流水自身 reservation_ref 为空，普通唯一约束会把多条空串判成重复。
        Index(
            "uq_budget_quota_ledger_reservation_kind",
            "reservation_ref",
            "transaction_kind",
            unique=True,
            postgresql_where=text("reservation_ref <> ''"),
        ),
        CheckConstraint(
            "transaction_kind IN (" + ", ".join(f"'{v}'" for v in transaction_kind_values()) + ")",
            name="ck_budget_ledger_transaction_kind",
        ),
        Index("ix_budget_quota_budget_ledger_tenant_id", "tenant_id"),
        status_check((s.value for s in NoLifecycleStatus), table="budget_ledger"),
        {"schema": SCHEMA_NAME},
    )

    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="CNY")
    transaction_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    reservation_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )


__all__ = ["SCHEMA_NAME", "BudgetLedgerModel", "ShotBudgetRequestModel"]
