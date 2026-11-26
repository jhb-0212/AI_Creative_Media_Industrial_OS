"""qc_governance 域 ORM 模型（自有 schema + 自有角色，DATA-1）。

为什么 Gate 决策独立成域
------------------------
决策是「这个 Gate 已经裁决过」的唯一证据，语义上**追加即不可变**：撤销结论要
记新决策，不能改写历史（改写会让「当时凭什么放行」永久丢失）。这条不变量由
两层执行：``qc_governance_role`` 对该表只有 SELECT/INSERT（数据库层拒绝改写），
``status_check`` 只允许 NO_LIFECYCLE 哨兵（schema 层拒绝把决策伪装成状态机）。

迁移 ``a0b1c2d3e4f5`` 是本文件的 DDL 事实源。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin, project_tenant_fk
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import NoLifecycleStatus, QCTaskStatus

SCHEMA_NAME = "qc_governance"


class GateDecisionModel(Base, EntityMixin):
    """gate_decisions：Gate 决策记录（追加即不可变，NO_LIFECYCLE）。"""

    __tablename__ = "gate_decisions"
    __table_args__ = (
        # 归属外键：本表此前对 projects **一条外键都没有**。Gate 决策是
        # 「这个 Gate 裁决过」的唯一证据，挂在幻影项目上的放行结论不可审计。
        project_tenant_fk("gate_decisions"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_gate_decisions_decision_controlled",
        ),
        Index("ix_qc_governance_gate_decisions_project_id", "project_id"),
        Index(
            "ix_qc_governance_gate_decisions_project_id_gate_key",
            "project_id",
            "gate_key",
        ),
        Index("ix_qc_governance_gate_decisions_tenant_id", "tenant_id"),
        status_check((s.value for s in NoLifecycleStatus), table="gate_decisions"),
        {"schema": SCHEMA_NAME},
    )

    gate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    gate_instance_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    constraint_snapshot_id: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    entry_criteria: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=""
    )
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    comment: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=""
    )
    required_artifacts: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=""
    )
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    reviewers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    ruleset_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class QCTaskModel(Base, EntityMixin):
    """qc_tasks：QC 待检队列（QUEUED/RUNNING/PASSED/REJECTED + 租约）。"""

    __tablename__ = "qc_tasks"
    __table_args__ = (
        # 归属外键：本表此前对 projects **一条外键都没有**。
        project_tenant_fk("qc_tasks"),
        Index("ix_qc_governance_qc_tasks_tenant_id", "tenant_id"),
        Index("ix_qc_governance_qc_tasks_status", "status"),
        Index(
            "uq_qc_governance_qc_tasks_generation_task",
            "generation_task_id",
            unique=True,
        ),
        status_check((s.value for s in QCTaskStatus), table="qc_tasks"),
        # 代次非负：领域层已拒绝负数，但绕过领域层的写（迁移/运维脚本）也要被库拦。
        CheckConstraint(
            "lease_generation >= 0",
            name="ck_qc_governance_qc_tasks_lease_generation_non_negative",
        ),
        {"schema": SCHEMA_NAME},
    )

    qc_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_owner: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    lease_expires_at: Mapped[Any | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 租约代次（fencing token）：与 entity_version / attempt_count 语义不同，
    # 见 domain/qc_task.py 的字段注释。库层非负约束在迁移里（防绕过领域层写负数）。
    lease_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qc_report_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )


__all__ = ["SCHEMA_NAME", "GateDecisionModel", "QCTaskModel"]
