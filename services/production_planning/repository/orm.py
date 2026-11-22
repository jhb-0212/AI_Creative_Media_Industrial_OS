"""production_planning 域 ORM 模型（自有 schema + 自有角色，DATA-1）。

为什么单独一个域（而不是继续挂在 project_planning）
----------------------------------------------------
生产路线与生成任务有自己的证据链：路线选择记录一旦 VERIFIED 就是「当时为什么
选这条路」的证据，生成任务记录是「谁在什么约束下生成了什么」的证据。它们与
剧本/镜头/规格的 DML 面不同（后者的核心是乐观锁状态推进），混在同一 schema
会得到一个「什么都能改」的宽角色，而收窄权限必须先在物理上分开。

域边界由**角色**执行，不只是目录约定：``production_planning_role`` 没有账本与
Gate 决策的任何权限，也没有 asset_service 的写权限——越界写是 permission
denied，不是代码评审的意见。

表结构严格对齐 C.3.1 信封列（EntityMixin）+ 领域聚合根字段；迁移
``a0b1c2d3e4f5`` 是本文件的 DDL 事实源，二者由 ``alembic check`` 保证无漂移。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)

SCHEMA_NAME = "production_planning"


class ProductionRouteModel(Base, EntityMixin):
    """production_routes：路线选择记录（VERIFIED 出生，REVOKED 终态）。"""

    __tablename__ = "production_routes"
    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", name="uq_production_routes_entity_project"),
        Index("ix_production_planning_production_routes_project_id", "project_id"),
        Index("ix_production_planning_production_routes_tenant_id", "tenant_id"),
        status_check((s.value for s in ProductionRouteStatus), table="production_routes"),
        {"schema": SCHEMA_NAME},
    )

    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    route_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    basis_ref: Mapped[str] = mapped_column(String(200), nullable=False)


class GenerationTaskModel(Base, EntityMixin):
    """generation_tasks：生成任务记录（REQUESTED→SUCCEEDED/FAILED）。"""

    __tablename__ = "generation_tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", name="uq_generation_tasks_entity_project"),
        Index("ix_production_planning_generation_tasks_project_id", "project_id"),
        Index("ix_production_planning_generation_tasks_tenant_id", "tenant_id"),
        status_check((s.value for s in GenerationTaskStatus), table="generation_tasks"),
        {"schema": SCHEMA_NAME},
    )

    generation_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    production_route_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    model_adapter_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    constraint_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    budget_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    # 租约：领取者、到期时间、领取次数（Phase 2，GENERATION.PROCESS）。
    lease_owner: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # 产物引用（Phase 3 后续）：成功任务把产物与质量标准版本落库，
    # QC 入队与「成功但漏入队」的对账扫描都以这两列为唯一依据。
    asset_version_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    quality_profile_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )


__all__ = ["SCHEMA_NAME", "GenerationTaskModel", "ProductionRouteModel"]
