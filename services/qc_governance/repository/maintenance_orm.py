"""``qc_maintenance`` 的维护工单与审计账本 ORM（WP-A）。

为什么工单是表而不是一个令牌字符串
--------------------------------
受控维护需要证明三件事：**谁申请的、谁批准的、针对哪个租户**。令牌只能证明
「有人拿到过它」，证明不了授权链；而授权链是「维护身份」能做的事的唯一正当性
来源。因此工单有自己的状态机（见 ``packages/shared/maintenance.py``），审批与
执行是两次不同的状态跃迁，中间不可能被同一个动作跨过去。

为什么审计账本单独一张表
----------------------
台账要能回答「被拒过几次、为什么被拒」。把拒绝写进日志文件，就不再能被
SQL 审计视图查到；写在工单表里，又要求「被拒」也占一条工单（而它根本没有
被批准过）。单独一张只追加的账本，既不改变工单事实，也不丢拒绝证据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.maintenance import WORK_ORDER_KINDS, WORK_ORDER_STATES
from packages.shared.orm import EntityMixin

SCHEMA_NAME = "qc_maintenance"
WORK_ORDER_TABLE = "maintenance_work_orders"
ACTION_LOG_TABLE = "maintenance_action_log"


def _in_list_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class MaintenanceWorkOrderModel(Base, EntityMixin):
    """一张工单 = 一次被授权的维护意图（类型 + 租户 + 幂等键）。"""

    __tablename__ = WORK_ORDER_TABLE
    __table_args__ = (
        # 幂等键：同一意图重复提交（客户端重试、审批系统重放）只能落一行。
        # 不用「先查再插」表达幂等——并发下那仍然是竞态；唯一键才是判据。
        UniqueConstraint("idempotency_key", name="uq_maintenance_work_order_idem"),
        CheckConstraint(
            f"kind IN ({_in_list_sql(WORK_ORDER_KINDS)})",
            name="ck_maintenance_work_order_kind",
        ),
        CheckConstraint(
            f"status IN ({_in_list_sql(WORK_ORDER_STATES)})",
            name="ck_maintenance_work_order_status",
        ),
        # 已批准就必须有批准人；反之未批准的工单不得携带批准人（避免「先填
        # approved_by 再慢慢改成 APPROVED」这种看起来已批过的中间态）。
        CheckConstraint(
            "(status IN ('APPROVED','RUNNING','SUCCEEDED','FAILED') "
            "AND approved_by IS NOT NULL AND approved_at IS NOT NULL) "
            "OR (status IN ('REQUESTED','CANCELLED') AND approved_by IS NULL)",
            name="ck_maintenance_work_order_approval",
        ),
        # REPAIR 必须给出目标任务；DETECT/RECONCILE 是全租户/全域范围，禁止
        # 携带单任务目标——那会让「检测」看起来像「针对某个任务的修复」。
        CheckConstraint(
            "(kind = 'REPAIR' AND target_task_id IS NOT NULL) "
            "OR (kind <> 'REPAIR' AND target_task_id IS NULL)",
            name="ck_maintenance_work_order_target",
        ),
        Index("ix_maintenance_work_order_tenant", "tenant_id", "created_at"),
        Index("ix_maintenance_work_order_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class MaintenanceActionLogModel(Base):
    """只追加的维护审计账本（成功、失败、拒绝都落一行）。"""

    __tablename__ = ACTION_LOG_TABLE
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('SUCCEEDED','FAILED','DENIED')",
            name="ck_maintenance_action_outcome",
        ),
        UniqueConstraint("attempt_id", name="uq_maintenance_action_attempt"),
        Index("ix_maintenance_action_log_work_order", "work_order_id"),
        Index("ix_maintenance_action_log_tenant", "tenant_id", "created_at"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ACTION_LOG_TABLE",
    "SCHEMA_NAME",
    "WORK_ORDER_TABLE",
    "MaintenanceActionLogModel",
    "MaintenanceWorkOrderModel",
]
