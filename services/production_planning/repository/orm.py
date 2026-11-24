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

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin, project_tenant_fk
from packages.shared.status_constraint import status_check
from packages.shared.storage_locator import sql_locator_check
from services.project_planning.domain.enums import (
    ArtifactManifestStatus,
    GenerationTaskStatus,
    ProductionRouteStatus,
)

SCHEMA_NAME = "production_planning"


class ProductionRouteModel(Base, EntityMixin):
    """production_routes：路线选择记录（VERIFIED 出生，REVOKED 终态）。"""

    __tablename__ = "production_routes"
    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", name="uq_production_routes_entity_project"),
        # 归属外键：本表此前对 projects **一条外键都没有**。路线选择是
        # 「当时为什么选这条路」的证据，指向幻影项目的证据链不成立。
        project_tenant_fk("production_routes"),
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
    # 任务创建时冻结的质量策略来源（C4）。空串表示该路线尚未绑定，
    # 创建准入须拒绝——判定在运行时服务层，列本身只保证事实有处可落。
    quality_profile_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )


class GenerationTaskModel(Base, EntityMixin):
    """generation_tasks：生成任务记录（REQUESTED→SUCCEEDED/FAILED）。"""

    __tablename__ = "generation_tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", name="uq_generation_tasks_entity_project"),
        # 产物清单与登记回执的复合外键目标：跨任务/跨租户伪造登记依据必须被拒。
        # 约束名由迁移 e1f2a3b4c5d6 建立，这里同步声明，避免 alembic check 漂移。
        UniqueConstraint(
            "generation_task_id", "project_id", "tenant_id",
            name="uq_generation_tasks_task_project_tenant",
        ),
        # 归属外键：本表此前对 projects **一条外键都没有**。生成任务是
        # 「谁在什么约束下生成了什么」的证据，其 project_id 必须指向真实项目。
        project_tenant_fk("generation_tasks"),
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


class ArtifactManifestModel(Base, EntityMixin):
    """artifact_manifests：产物写入意图（ADR-R42 T0/T1）。

    这条记录是崩溃恢复的权威依据：T0 提交后即使进程立即退出，也能按
    PREPARED 记录重试对象写入，而不必重新调用模型。``locator`` 与
    ``sha256`` 的绑定由数据库 CHECK 保证（与 asset_versions 同源）。
    """

    __tablename__ = "artifact_manifests"
    __table_args__ = (
        UniqueConstraint(
            "generation_task_id", "attempt_id",
            name="uq_artifact_manifests_task_attempt",
        ),
        UniqueConstraint(
            "project_id", "entity_id", name="uq_artifact_manifests_entity_project"
        ),
        project_tenant_fk("artifact_manifests"),
        ForeignKeyConstraint(
            ["generation_task_id", "project_id", "tenant_id"],
            [
                "production_planning.generation_tasks.generation_task_id",
                "production_planning.generation_tasks.project_id",
                "production_planning.generation_tasks.tenant_id",
            ],
            name="fk_artifact_manifests_generation_task",
        ),
        Index("ix_artifact_manifests_project_id", "project_id"),
        Index("ix_production_planning_artifact_manifests_tenant_id", "tenant_id"),
        Index("ix_artifact_manifests_generation_task_id", "generation_task_id"),
        status_check(
            (s.value for s in ArtifactManifestStatus), table="artifact_manifests"
        ),
        CheckConstraint(
            sql_locator_check("locator"), name="ck_artifact_manifests_locator_sha256"
        ),
        {"schema": SCHEMA_NAME},
    )

    generation_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    locator: Mapped[str] = mapped_column(String(96), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # PR-G：T0 固化的媒体类型，与 locator 扩展名同源。T1/T2 与 PR-G 恢复都以它
    # 为落盘事实，不再只依赖 T1 事件载荷（事件丢失/篡改时仍可核对）。
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "SCHEMA_NAME",
    "ArtifactManifestModel",
    "GenerationTaskModel",
    "ProductionRouteModel",
]
