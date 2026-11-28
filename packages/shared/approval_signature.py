"""累积式会签签署事实（``audit.approval_signatures``）——推进前的席位签署事实。

为什么与 ``status_transition`` 分表（ADR-R23 §D2-e / ADR-R45）
--------------------------------------------------------------
``status_transitions`` 记录「推进**已经**发生」，``CHECK (from_status <> to_status)``
把 no-op 堵死；会签需要的是「推进**之前**某席位签了字」——另一类事实，有独立
生命周期（撤回 / 消费 / 凑不齐）。两类事实挤进一张表 = 用判别列代替建模，
必然腐化。因此独立成表，本模块是它的唯一 ORM 映射与写入值对象。

三态（所有者 R3 裁定：SIGNED / REVOKED / CONSUMED）
---------------------------------------------------
append-only 事件日志：状态流转以**新 event_kind 行**表达，不原地 UPDATE。
- SIGNED：某主体对某席位签署；
- REVOKED：签署人撤回（与 SIGNED 同 seat/subject，不同 event_kind）；
- CONSUMED：凑齐放行时整组写一条，使该组签署一次性失效、不可重放。

不变量（交给数据库，不靠 ORM 自觉）
-----------------------------------
append-only 由迁移触发器保证；唯一约束 ``uq_signature_event`` 保证同席位同主体
不可重复签；``ck_signature_kind`` 限定三态；``ck_signature_seat_positive`` 限定
席位序号 >= 1。ORM 只是映射与写入值对象，不重复这些规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base

SCHEMA_NAME = "audit"


class SignatureEventKind(str, Enum):
    """签署生命周期三态（与状态机契约 ApprovalSignature 一致）。"""

    SIGNED = "SIGNED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


class ApprovalSignatureModel(Base):
    """``audit.approval_signatures`` 表映射（append-only）。

    约束名与迁移 ``e4f5a6b7c8d9`` 逐字一致，避免 autogenerate 漂移。
    """

    __tablename__ = "approval_signatures"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "resource_type",
            "resource_id",
            "action_key",
            "seat_index",
            "subject",
            "event_kind",
            name="uq_signature_event",
        ),
        CheckConstraint(
            "event_kind IN ('SIGNED','REVOKED','CONSUMED')",
            name="ck_signature_kind",
        ),
        CheckConstraint("seat_index >= 1", name="ck_signature_seat_positive"),
        Index(
            "ix_approval_signatures_group",
            "tenant_id",
            "resource_type",
            "resource_id",
            "action_key",
            "occurred_at",
        ),
        {"schema": SCHEMA_NAME},
    )

    signature_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)
    seat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    authority_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


@dataclass(frozen=True)
class SignatureEvent:
    """写入侧的一次签署事件值对象（不可变）。

    ``subject`` 必须来自 Principal（不可伪），不进请求体；请求体里若需溯源可留
    ``operator_ref``，但它不参与任何比较（R43 身份不可伪铁律）。
    """

    tenant_id: UUID
    resource_type: str
    resource_id: str
    action_key: str
    seat_index: int
    subject: str
    event_kind: SignatureEventKind
    organization_id: UUID | None = None
    authority_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    signature_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: UUID | None = None


def describe_signature(event: SignatureEvent) -> dict[str, Any]:
    """签署事件的只读视图（供测试与审计读接口复用，不各处手拼字段）。"""
    return {
        "signature_id": str(event.signature_id),
        "tenant_id": str(event.tenant_id),
        "organization_id": (
            str(event.organization_id) if event.organization_id is not None else None
        ),
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "action_key": event.action_key,
        "seat_index": event.seat_index,
        "subject": event.subject,
        "event_kind": event.event_kind.value,
        "authority_ref": event.authority_ref,
        "evidence_refs": list(event.evidence_refs),
        "occurred_at": event.occurred_at.isoformat(),
        "event_id": str(event.event_id) if event.event_id is not None else None,
    }


__all__: list[str] = [
    "SCHEMA_NAME",
    "ApprovalSignatureModel",
    "SignatureEvent",
    "SignatureEventKind",
    "describe_signature",
]
