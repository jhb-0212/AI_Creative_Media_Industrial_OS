"""资产域 ORM 模型（独立 schema：asset_service）。

数据所有权（14.2）：
- asset_service 模块拥有 asset_versions / qc_reports 两张表的写入权。
- 独立 schema「asset_service」，不写进 project_planning，避免跨模块抢表。
- 信封列复用 packages.shared.orm.EntityMixin（跨模块单一事实源）。
- tenant_id 是 RLS 隔离键（决策 5），与信封一同落库。

归属完整性（SCOPE-3，第二轮审查 P1-04 根治）：
- asset_versions.project_id 是**唯一**归属键，指向
  project_planning.projects(entity_id, tenant_id) 的复合外键：资产必然属于一个
  真实存在、且与资产同租户的项目，在数据库层成立，不靠 Service 查询兜底。
- 组织归属**不落库**：organization 由 project 派生（project → organization）。
  冗余存一列会在「项目改归属组织」时产生第二事实源——这正是第三轮审查指认
  的 audit_ref 式漂移面，不能重犯。
- 镜头级资产是「被镜头引用」而非「归属镜头」：action_registry 的
  scope: project/shot/asset 描述的是**授权作用域**，不是归属层级。引用关系在
  Shot 域落地时以引用表表达，不能在此用多态归属列冒充（多态键无法建外键）。

为什么没有 asset_version_id 列：
- 契约对象同时暴露 entity_id 与 asset_version_id；本实现让两者同值，
  只持久化 entity_id 一个主键，序列化时派生 asset_version_id。两个同值
  主键列会制造「改一个忘一个」的漂移面，不是约束力而是负债。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin, project_tenant_fk
from packages.shared.status_constraint import status_check
from packages.shared.storage_locator import sql_locator_check
from services.asset_service.domain.enums import AssetStatus

SCHEMA_NAME = "asset_service"
# 归属目标（跨 schema 只引用，不拥有）由 packages.shared.orm.OWNERSHIP_ROOT
# 统一声明，本模块不再自带一份——同一个事实写在两处，改一处就会不一致。


class AssetVersionModel(EntityMixin, Base):
    """asset_versions 表模型（受控资产版本）。"""

    __tablename__ = "asset_versions"
    __table_args__ = (
        # 资产类型 + 摘要索引：按「同类型同内容」快速定位，供去重/溯源查询。
        Index("ix_asset_service_asset_versions_type_sha256", "asset_type", "sha256"),
        # 归属索引：按项目列举资产（授权与业务查询共用）。
        Index("ix_asset_service_asset_versions_project", "project_id"),
        # 复合外键：资产所属项目必须与资产同租户（跨租户挂靠被数据库拒绝）。
        # 构造已收敛到 packages.shared.orm.project_tenant_fk（全平台唯一出处）。
        project_tenant_fk("asset_versions"),
        # status 收口（十态）：资产版本是唯一允许「GOLD / QUARANTINED」等旁支的
        # 对象，库里若只认有入边的五态，真实数据一出现旁支就会被 CHECK 打回。
        status_check((s.value for s in AssetStatus), table="asset_versions"),
        # storage_ref 必须是受控内容寻址定位符（P1-07）。
        #
        # 为什么这条不变量必须落在数据库而不是只靠服务层：产物由多个写入口
        # 落库（worker、运维脚本、未来的新用例），服务层校验只覆盖经过它的
        # 那条路径。记忆里「策略 ≠ 约束」那一条说的就是这件事——应用层校验
        # 看起来等价，但它不在所有连接的必经之路上。
        #
        # 表达式来自 packages/shared/storage_locator.sql_locator_check()：
        # PostgreSQL 正则不支持反向引用，所以 SQL 侧用「形态正则 + 位置算术」
        # 两段合取表达同一条判据。三处表达（Python/契约/SQL）的等价性由
        # tests/integration/test_storage_locator_constraint.py 用同一批样本锁定。
        CheckConstraint(
            sql_locator_check("storage_ref"),
            name="ck_asset_versions_storage_ref_locator",
        ),
        {"schema": SCHEMA_NAME},
    )

    # 归属键（唯一）：资产属于哪个项目。
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)

    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 受控定位符（受 CHECK 约束），不是自由 JSON。长度上限取形态的最大值：
    # "local://" (8) + 2 + 1 + 2 + 1 + 64 + 1 + 5 = 84，留余量取 96。
    storage_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    source_asset_version_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    rights_manifest_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adapter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_product: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class QCReportModel(Base):
    """qc_reports 表模型（不可变检测证据）。

    外键 ``asset_version_id → asset_versions.entity_id`` 在数据库层锁死
    「报告挂在不存在的资产上」这类悬空引用；仅靠服务层查询校验不够。
    """

    __tablename__ = "qc_reports"
    __table_args__ = {"schema": SCHEMA_NAME}

    qc_report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(f"{SCHEMA_NAME}.asset_versions.entity_id"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    quality_profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    benchmark_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detector_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    findings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    deviation_class: Mapped[str | None] = mapped_column(String(24), nullable=True)
    deviation_intent_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intent_match_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    human_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
