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

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base

# 独立 schema 名称（平台级事件总线，跨领域共享）。
SCHEMA_NAME = "outbox"

# 死信重放审计的 result 允许集合：**只有 SUCCEEDED 一个值**。
#
# 建表时（c5d6e7f8a9b0）写的是 ``result IN ('SUCCEEDED', 'FAILED')``，理由是
# 「留待将来多步重放」。实际上本实现的重放是「锁行 → 条件清标记 → 写审计」的
# 单事务，全有或全无，不存在「已受理但失败」的中间态：任何失败都在写之前以
# DomainError 拒绝，落库就一定是成功。于是 FAILED 是一个**永远写不进去的枚举值**。
#
# 保留它的代价不是「多一个值」，而是读代码的人会以为失败路径漏写了，进而去
# 补一条本不该存在的失败分支（或在别处 import 这个值做判定）。虚假的可选值
# 比缺一个值更贵：缺值会报错，假值会误导。将来真出现多步重放时，加值与给出
# 失败路径必须是**同一次**改动，而不是现在先把值留着。
#
# 这段表达式同时被 ORM 与迁移 f8a9b0c1d2e3 引用，避免两处手写文本漂移。
DEAD_LETTER_REPLAY_SUCCEEDED = "SUCCEEDED"
DEAD_LETTER_REPLAY_RESULT_CHECK = f"result = '{DEAD_LETTER_REPLAY_SUCCEEDED}'"


class OutboxEventModel(Base):
    """outbox_events 表模型（不可变审计事件，append-only）。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        # 索引名与迁移 7c8d9e0f1a2b 保持一致，避免 autogenerate 漂移。
        Index("ix_outbox_aggregate", "aggregate_id"),
        Index("ix_outbox_published", "published_at"),
        Index("ix_outbox_tenant", "tenant_id"),
        # 死信列表的部分索引（迁移 f9b0c1d2e3f4）：死信只占全表极小比例，
        # 全表索引会让写入为极少数行付代价；条件索引进而把排序也一并覆盖——
        # 死信查询固定按 dead_lettered_at 倒序、event_id 倒序做键集分页。
        Index(
            "ix_outbox_dead_letter",
            "dead_lettered_at",
            "event_id",
            postgresql_where=text("dead_lettered_at IS NOT NULL"),
        ),
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
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 投递重试与死信（P2-2 / 迁移 f3a4b5c6d7e8）：
    # attempts 记录失败次数，next_attempt_at 控制指数退避，
    # dead_lettered_at 非空即"已放弃投递"（不再被 CLAIM 领取，但保留可查）。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DeadLetterReplayModel(Base):
    """outbox.dead_letter_replays（死信人工重放的 append-only 审计）。

    为什么审计不落 outbox_events（推翻「复用 T2 先例」的直觉）
    ------------------------------------------------------------
    T2 的冲突证据能复用 outbox_events，前提是写它的主体是业务域角色
    （``production_planning_role`` 本就持有 ``outbox_events INSERT``）。重放
    由**运维主体**执行，而 ``outbox_relay_role`` 对 outbox_events 只有
    SELECT + 列级 UPDATE、没有 INSERT——给运维角色补 INSERT，等于允许它伪造
    任意业务事件，是权限倒退。所以审计载体必须是独立表：可写审计、不可写业务
    事件，这是权限边界决定的，不是「多建一张表更保险」。

    append-only 的落地方式有两层，缺一层都不够：
    1. 授权层：``outbox_ops_role`` 只有 SELECT/INSERT，没有 UPDATE/DELETE；
    2. 触发器层：``BEFORE UPDATE OR DELETE`` 与 ``BEFORE TRUNCATE`` 直接抛错。
    只做授权层时，超级用户或将来某个误配的角色仍能改写历史；只做触发器层时，
    「能不能写」这件事不再由 GRANT 表达。两层同时存在，才既拦得住也说得清。

    与 ``maintenance_runs`` 同一个口径：运维台账**不登记**为契约对象
    （不在 object_schemas / state_machines 里），因为它不是业务聚合。
    """

    __tablename__ = "dead_letter_replays"
    __table_args__ = (
        Index("ix_outbox_dead_letter_replays_source_event", "source_event_id"),
        Index("ix_outbox_dead_letter_replays_tenant_created", "tenant_id", "created_at"),
        CheckConstraint(
            DEAD_LETTER_REPLAY_RESULT_CHECK,
            name="ck_dead_letter_replays_result",
        ),
        {"schema": SCHEMA_NAME},
    )

    # 操作身份，不是事件身份：标识「某人在某时对某条死信做过一次重放」。
    # 业务幂等键仍是 event_id（重放不生成新事件），两者不能混用。
    replay_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # 留一份事件类型：分区被摘除归档后，审计行仍要能回答「重放的是什么」。
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 重放**前**的失败事实。先写审计再清零 attempts，顺序反了这两个值就永久丢失。
    attempts_before: Mapped[int] = mapped_column(Integer, nullable=False)
    dead_lettered_at_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MaintenanceRunModel(Base):
    """outbox.maintenance_runs（保留期维护的审计与新鲜度表）。

    与迁移 b8c9d1e2f3a4 一一对应：该表存在的主要理由是让「上次成功维护时间」
    可被查询与告警，而不是依赖某个人记得跑 CLI。它不属于租户数据，不启用 RLS。
    """

    __tablename__ = "maintenance_runs"
    __table_args__ = (
        Index("ix_outbox_maintenance_runs_started", "started_at"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created: Mapped[int] = mapped_column(Integer, nullable=False)
    detached: Mapped[int] = mapped_column(Integer, nullable=False)
    refused: Mapped[int] = mapped_column(Integer, nullable=False)
    # ok / refused / error / dry_run
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
