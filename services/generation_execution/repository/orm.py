"""Generation Execution ORM 模型（映射到独立 schema：generation_execution）。

数据所有权（V2.0.5 14.2 / ADR-R27 范式）：
- generation_execution 模块拥有 ProductionRoute / GenerationTask 表的写入权；
- 使用独立 schema「generation_execution」与独立数据库角色
  svc_generation_execution（packages/shared/db_roles.py）；
- 表结构严格对齐 C.3.1 通用实体信封字段（信封列收敛到 EntityMixin）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "generation_execution"


class ProductionRouteModel(EntityMixin, Base):
    """production_routes 表模型（生产路线适配器目录条目，A6）。"""

    __tablename__ = "production_routes"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段（契约字段集：A6 冻结定义，无 project_id——路线是平台能力
    # 目录条目，不是项目资源；RLS 走租户隔离）。
    route_type: Mapped[str] = mapped_column(String(32), nullable=False)
    supported_control_signals: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    required_appearance_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    qc_dimensions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    capability_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 验证元数据（16.2：VERIFIED 必须有证据背书；契约无此字段，
    # 与 ShotSpec 锁定元数据同范式——内部列不出契约响应）。
    verified_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProductionRouteStatusTransitionModel(Base):
    """`production_route_status_transitions` 表模型（只读参考数据，ADR-R20）。

    这张表是 ProductionRoute 状态机在数据库层的表达：触发器守卫按它判定
    `status` 变化是否合法。不属于领域聚合，业务代码不可写入——应用角色
    只有 SELECT，写入只发生在迁移里。声明成模型让 alembic check 能校验
    这张表的结构。
    """

    __tablename__ = "production_route_status_transitions"
    __table_args__ = {"schema": SCHEMA_NAME}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)


class GenerationTaskModel(EntityMixin, Base):
    """generation_tasks 表模型（生成任务，A6 冻结字段集）。

    retry_of / result_ref / rights_manifest_ref / 租约列是实现扩展列（契约
    additionalProperties 允许）：
    - retry_of：重试链锚点（重试以新任务承载，9.4）；
    - result_ref：产出对象引用（登记成功时指向 AssetVersion，ADR-R33）；
    - rights_manifest_ref：产出权利凭据引用（资产批准链的前置，ADR-R33）；
    - lease_owner / lease_expires_at：执行租约（崩溃恢复的唯一事实，ADR-R34）。
    """

    __tablename__ = "generation_tasks"
    __table_args__ = (
        # 过期租约扫描的部分索引：回收只看"在跑"的任务，把已终态的历史行
        # 排除在索引之外（ADR-R34）。声明在模型里，alembic check 才能校验
        # 它与迁移一致——只写在迁移里的索引会被当成"待删除"。
        Index(
            "ix_generation_tasks_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段（契约字段集：A6 冻结定义；生成任务是项目级资源，带 project_id）。
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_spec_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    visual_plan_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    constraint_snapshot_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_reservation_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    quota_reservation_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False)
    retry_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 产出权利凭据引用（受理时定型；不可变内容列——准入事实不可事后改写）。
    rights_manifest_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    result_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # worker 转移伴随元数据（守卫可变白名单列；不出契约响应）。
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 执行租约（ADR-R34）：运行态元数据，接管/续租改写；不出契约响应。
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GenerationTaskStatusTransitionModel(Base):
    """`generation_task_status_transitions` 表模型（只读参考数据，ADR-R20）。

    与 ``production_route_status_transitions`` 同范式：触发器守卫的判定
    依据，应用角色只有 SELECT，写入只发生在迁移里。
    """

    __tablename__ = "generation_task_status_transitions"
    __table_args__ = {"schema": SCHEMA_NAME}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)
