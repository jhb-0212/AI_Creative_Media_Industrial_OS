"""Transactional Outbox 基础设施（决策 3：审计事件不可变、状态与事件同事务）。

设计要点（不屎山代码 + R7 状态可追溯）：
- 审计事件不再是「单字段 audit_ref 覆盖」，而是 append-only 的不可变事件流。
- 业务状态变更与事件写入在**同一数据库事务**内原子完成（Transactional
  Outbox 模式）：状态改对了、事件就一定有；状态回滚、事件也一并回滚。
- 事件负载（payload）记录 before/after 状态、操作主体、理由、关联标识，
  使审批/取代等历史过程可完整恢复（R7）。
- 后续后台 Worker 读取 published_at IS NULL 的事件，异步投递到
  Kafka/RabbitMQ 或分析型审计表，主库保持轻量。

数据所有权：outbox 属平台级事件总线，使用独立 schema「outbox」，供所有
领域模块共享写入，而非 project_planning 专属。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base

# 独立 schema 名称（平台级事件总线，跨领域共享）。
SCHEMA_NAME = "outbox"


class OutboxEventModel(Base):
    """outbox_events 表模型（不可变审计事件，append-only）。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        # 索引名与迁移 7c8d9e0f1a2b 保持一致，避免 autogenerate 漂移。
        Index("ix_outbox_aggregate", "aggregate_id"),
        Index("ix_outbox_published", "published_at"),
        Index("ix_outbox_tenant", "tenant_id"),
        {"schema": SCHEMA_NAME},
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # 注：物理表按 occurred_at 做月分区，PostgreSQL 要求唯一约束包含分区键，
    # 实际主键是 (event_id, occurred_at)。ORM 侧仍以 event_id 为映射主键
    # （单条事件语义不变），分区布局见 ADR-R06-2-outbox-retention-partitioning。
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 租户归属（决策 5 / R06）：事件同样受 RLS 隔离，审计数据不得跨租户读取。
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 已投递标记：NULL 表示未投递，Worker 消费后写入投递时间。
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
