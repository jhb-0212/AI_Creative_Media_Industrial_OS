"""通用审批引擎 ORM 模型（独立 schema `workflow`，独立数据库角色）。

三张表的权限口径不同，必须分开看：
- `approval_flows` / `approval_steps`：append-only，只授 SELECT/INSERT + 触发器；
- `approval_tickets`：状态投影可推进，UPDATE 只授状态相关列（见迁移）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

SCHEMA_NAME = "workflow"

FLOW_MODES = ("ALL", "ANY")
TICKET_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
VOTE_DECISIONS = ("APPROVE", "REJECT")


class ApprovalFlowModel(EntityMixin, Base):
    __tablename__ = "approval_flows"
    __table_args__ = (
        Index("ix_workflow_approval_flows_action_key", "action_key"),
        Index("ix_workflow_approval_flows_resource", "resource_type", "resource_id"),
        CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in FLOW_MODES)})",
            name="ck_workflow_approval_flows_mode_registered",
        ),
        {"schema": SCHEMA_NAME},
    )

    action_key: Mapped[str] = mapped_column(String(96), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(96), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(96), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    groups: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    ruleset_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ApprovalTicketModel(EntityMixin, Base):
    __tablename__ = "approval_tickets"
    __table_args__ = (
        Index("ix_workflow_approval_tickets_flow_id", "flow_id"),
        Index("ix_workflow_approval_tickets_project", "project_id"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in TICKET_STATUSES)})",
            name="ck_workflow_approval_tickets_status_registered",
        ),
        CheckConstraint(
            "status = 'PENDING' OR completed_at IS NOT NULL",
            name="ck_workflow_approval_tickets_terminal_has_time",
        ),
        {"schema": SCHEMA_NAME},
    )

    flow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # status 复用 EntityMixin 的通用信封列（不重复声明）。
    # 领域版号（乐观锁），与信封的 entity_version 独立：票据状态推进不产生新实体版本。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApprovalStepModel(EntityMixin, Base):
    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id",
            "group_name",
            "voter_subject_id",
            name="uq_workflow_approval_steps_one_vote_per_group",
        ),
        Index("ix_workflow_approval_steps_ticket_id", "ticket_id"),
        CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in VOTE_DECISIONS)})",
            name="ck_workflow_approval_steps_decision_registered",
        ),
        CheckConstraint(
            "voter_subject_type <> 'agent'",
            name="ck_workflow_approval_steps_no_agent_voter",
        ),
        CheckConstraint(
            "decision <> 'REJECT' OR length(btrim(reason)) > 0",
            name="ck_workflow_approval_steps_reject_needs_reason",
        ),
        {"schema": SCHEMA_NAME},
    )

    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_name: Mapped[str] = mapped_column(String(64), nullable=False)
    voter_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    voter_subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "FLOW_MODES",
    "SCHEMA_NAME",
    "TICKET_STATUSES",
    "VOTE_DECISIONS",
    "ApprovalFlowModel",
    "ApprovalStepModel",
    "ApprovalTicketModel",
]
