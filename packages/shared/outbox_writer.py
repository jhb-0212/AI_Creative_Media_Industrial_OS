"""Transactional Outbox 写入辅助（领域无关，供各领域服务层调用）。

设计要点（消除重复机制 R4）：
- 各领域服务在状态迁移后，需「在同一事务内追加一条不可变审计事件」。
  若让每个服务手写 ``OutboxEventModel(...) + session.add(...)``，会散落
  重复构造逻辑；收敛为单一 ``record_event`` 函数。
- 不 commit：事务边界由调用方的工作单元（``packages.shared.unit_of_work``）统一
  控制，本函数仅 ``session.add``，随工作单元一起提交/回滚。
- 事件类型（event_type）对齐契约 action_registry.yaml 的 audit_event 字段
  （如 SCRIPT_IMPORTED / SCRIPT_LOCK_APPROVED / SCRIPT_SUPERSEDED）。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.outbox import OutboxEventModel
from packages.shared.status_transition import (
    StatusTransitionModel,
    TransitionEvent,
    TransitionFact,
)


class OutboxWriter(ABC):
    """审计事件写入端口（领域服务依赖的抽象）。

    领域服务不直接依赖 SQLAlchemy session，而是依赖本端口，与仓储端口同构，
    保证服务层可用内存 fake 替换（单测无数据库），也可演进为其他实现
    （如 Kafka 直投）。实现方只 add，不 commit。
    """

    @abstractmethod
    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> uuid.UUID:
        """登记一条不可变审计事件（不 commit），返回事件 id。"""
        raise NotImplementedError

    @abstractmethod
    def record_transition(
        self, *, fact: TransitionFact, event: TransitionEvent
    ) -> uuid.UUID:
        """把「迁移事实 + 事件」作为一对互指记录写入（同一事务，不 commit）。

        为什么合成一个方法而不是两个调用：两者缺一即为非法状态——事实的
        ``event_id`` 非空、事件 payload 存 ``transition_id``。拆成两个方法就等于
        允许「只写一条」，而「只写一条」恰恰是这张表要消除的失败形态。
        归档 outbox 分区不影响事实可查，事件也不再被要求承担历史职责。
        """
        raise NotImplementedError


class SqlAlchemyOutboxWriter(OutboxWriter):
    """基于 SQLAlchemy 会话的审计事件写入实现（与业务变更共享同一事务）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> uuid.UUID:
        return record_event(
            self._session,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
        )

    def record_transition(
        self, *, fact: TransitionFact, event: TransitionEvent
    ) -> uuid.UUID:
        """写入互指的一对记录：事件（含 transition_id）+ 事实（含 event_id）。

        事件先写以拿到 ``event_id``（迁移侧的提交时校验要求它真实存在），
        事实随后写入并持有该 id。两条记录都在调用方的工作单元里，一起提交或回滚。
        """
        event_id = self.record(
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            tenant_id=event.tenant_id,
            event_type=event.event_type,
            payload={**event.payload, "transition_id": str(fact.transition_id)},
        )
        self._session.add(
            StatusTransitionModel(
                transition_id=fact.transition_id,
                tenant_id=fact.tenant_id,
                organization_id=fact.organization_id,
                resource_type=fact.resource_type,
                resource_id=fact.resource_id,
                entity_version=fact.entity_version,
                from_status=fact.from_status,
                to_status=fact.to_status,
                owner_action=fact.owner_action,
                actor_subject=fact.actor_subject,
                authority_ref=fact.authority_ref,
                evidence_refs=list(fact.evidence_refs),
                occurred_at=fact.occurred_at,
                event_id=event_id,
            )
        )
        return event_id



def record_event(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """在当前事务内追加一条不可变审计事件（不 commit）。

    参数：
    - session：当前工作单元会话（与业务状态变更共享同一事务）。
    - aggregate_type：聚合根类型（Project/ScriptRevision/Episode）。
    - aggregate_id：聚合根 entity_id。
    - event_type：事件类型（对齐契约 audit_event，如 SCRIPT_LOCK_APPROVED）。
    - payload：事件负载（before/after 状态、操作主体、理由、关联标识等）。

    返回：新事件的 event_id（供调用方关联/测试断言）。
    """
    event_id = uuid.uuid4()
    session.add(
        OutboxEventModel(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            occurred_at=datetime.now(UTC),
            published_at=None,
        )
    )
    return event_id
