"""状态迁移事实（``audit.status_transitions``）——审批可追溯的第一类事实。

为什么需要它（根因，不是「多一张表」）
--------------------------------------
一次推进此前只留两处痕：实体上会被下次迁移覆盖的 ``audit_ref``，与按保留期
DETACH 的 ``outbox.outbox_events``。于是「谁在什么证据下批准了什么」在保留期之后
不可查询——事实的生命周期由**传输机制**的保留策略决定了。ADR-R23 的判据是：
事实的生命周期必须由事实的用途决定。

本模块提供三样东西，各自只有一件职责：

1. ``StatusTransitionModel``：表映射（append-only 由迁移里的触发器保证，
   不靠 ORM 自觉）。
2. ``TransitionFact``：写入侧的事实值对象（不可变）。用值对象而不是长参数表，
   因为「少写一个字段」在长参数表里是静默的，在 dataclass 里会直接报错。
3. ``transition_audit_ref`` / ``classify_audit_ref``：``audit_ref`` 从「自由字符串
   凭据摘要」降级为「指向某条事实的指针」后的**唯一**编解码口径。写侧只产生
   ``tr:<uuid>``；读侧的遗留值（迁移前的 ``ASSET.STATUS.CHANGE:director_1`` 之类）
   必须被识别为 ``legacy`` 而不是解析失败——否则升级当天所有历史记录都会被判损坏。

与事件的关系是**互指**而非依赖：事实存 ``event_id``，事件 payload 存
``transition_id``。所以归档 outbox 分区不影响审计可查询性，而事件表也不再被
要求承担历史职责。
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

# 证据 schema：与 outbox 同为「跨域写入、单一所有者」。
SCHEMA_NAME = "audit"

# audit_ref 指针前缀。前缀而不是「时间戳分界」：updated_at 可被业务改、时钟不可靠，
# 而写在值里的前缀是自描述的，不会撒谎。
AUDIT_REF_POINTER_PREFIX = "tr:"


class StatusTransitionModel(Base):
    """``audit.status_transitions`` 表映射（append-only）。

    约束名与迁移 ``d8e9f0a1b2c3`` 逐字一致，避免 autogenerate 漂移。
    """

    __tablename__ = "status_transitions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "resource_type",
            "resource_id",
            "entity_version",
            name="uq_status_transition_version",
        ),
        CheckConstraint("from_status <> to_status", name="ck_status_transition_advances"),
        Index(
            "ix_status_transitions_resource_history",
            "tenant_id",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
        {"schema": SCHEMA_NAME},
    )

    transition_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    organization_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 与实体表 entity_id 同型（varchar(64)）：不假定资源标识必须是 UUID，
    # 否则事实表会反过来限制它能观测的对象。
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str] = mapped_column(String(64), nullable=False)
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(128), nullable=False)
    authority_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 非空：无事件的迁移不存在（迁移侧另有提交时校验，两者缺一不可——
    # NOT NULL 挡「忘传」，约束触发器挡「传了个不存在的」）。
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


@dataclass(frozen=True)
class TransitionFact:
    """一次状态推进的事实值对象（写入侧）。

    字段顺序按「必须知道」优先；带默认值的都是可选事实，缺了不算撒谎，
    但不可省略的（谁、从哪到哪、哪条边）没有默认值——漏传直接 TypeError。
    """

    tenant_id: UUID
    resource_type: str
    resource_id: str
    entity_version: int
    from_status: str
    to_status: str
    owner_action: str
    actor_subject: str
    organization_id: UUID | None = None
    authority_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    transition_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: UUID | None = None


class AuditRefKind(str, Enum):
    """``audit_ref`` 的三种形态（ADR-R23 §D2-c 的三态判定）。"""

    ABSENT = "absent"
    POINTER = "pointer"
    LEGACY = "legacy"


def transition_audit_ref(transition_id: UUID) -> str:
    """把事实 id 编码成实体 ``audit_ref`` 指针。写侧只有这一个入口。"""
    return f"{AUDIT_REF_POINTER_PREFIX}{transition_id}"


def classify_audit_ref(value: str | None) -> AuditRefKind:
    """判定 ``audit_ref`` 属于哪一态。

    刻意只按前缀判别：迁移前的自由串（``ASSET.STATUS.CHANGE:director_1``）一律
    ``LEGACY``，**不尝试解析、不判错**。要求「每个非空 audit_ref 都必须解析到
    transition 行」会把合法历史判成损坏——§5 已明确不回填历史，那条要求与它矛盾。
    """
    if value is None or not value.strip():
        return AuditRefKind.ABSENT
    if value.startswith(AUDIT_REF_POINTER_PREFIX):
        return AuditRefKind.POINTER
    return AuditRefKind.LEGACY


def parse_audit_ref(value: str) -> UUID | None:
    """从 ``tr:<uuid>`` 取出事实 id；非指针或格式非法返回 ``None``。

    返回 ``None`` 不等于「数据损坏」：调用方须先用 ``classify_audit_ref`` 区分
    ``LEGACY``（合法历史、不解析）与 ``POINTER``（必须解析成功）。两者混为一谈
    正是原稿的 P1 缺陷。
    """
    if classify_audit_ref(value) is not AuditRefKind.POINTER:
        return None
    raw = value[len(AUDIT_REF_POINTER_PREFIX) :].strip()
    try:
        return UUID(raw)
    except ValueError:
        return None


def explain_transition(fact: TransitionFact) -> dict[str, Any]:
    """事实的只读视图（供测试与将来的读接口复用，避免各处手拼字段）。"""
    return {
        "transition_id": str(fact.transition_id),
        "tenant_id": str(fact.tenant_id),
        "organization_id": (
            str(fact.organization_id) if fact.organization_id is not None else None
        ),
        "resource_type": fact.resource_type,
        "resource_id": fact.resource_id,
        "entity_version": fact.entity_version,
        "from_status": fact.from_status,
        "to_status": fact.to_status,
        "owner_action": fact.owner_action,
        "actor_subject": fact.actor_subject,
        "authority_ref": fact.authority_ref,
        "evidence_refs": list(fact.evidence_refs),
        "occurred_at": fact.occurred_at.isoformat(),
        "event_id": str(fact.event_id) if fact.event_id is not None else None,
    }


@dataclass(frozen=True)
class TransitionEvent:
    """与事实同事务写入的事件（降级为「通知下游」）。

    单独定义而不是复用 ``record()`` 的长参数表：``record_transition`` 需要同时
    拿到事件与事实，才能保证两者互指。
    """

    aggregate_type: str
    aggregate_id: str
    tenant_id: UUID
    event_type: str
    payload: dict[str, Any]
