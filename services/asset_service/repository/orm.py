"""资产域 ORM 模型（独立 schema：asset_service）。

数据所有权（14.2）：
- asset_service 模块拥有 asset_versions / qc_reports 两张表的写入权。
- 独立 schema「asset_service」，不写进 project_planning，避免跨模块抢表。
- 信封列复用 packages.shared.orm.EntityMixin（跨模块单一事实源）。
- tenant_id 是 RLS 隔离键（决策 5），与信封一同落库。

为什么没有 asset_version_id 列：
- 契约对象同时暴露 entity_id 与 asset_version_id；本实现让两者同值，
  只持久化 entity_id 一个主键，序列化时派生 asset_version_id。两个同值
  主键列会制造「改一个忘一个」的漂移面，不是约束力而是负债。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

SCHEMA_NAME = "asset_service"


class AssetVersionModel(EntityMixin, Base):
    """asset_versions 表模型（受控资产版本）。"""

    __tablename__ = "asset_versions"
    __table_args__ = (
        # 资产类型 + 摘要索引：按「同类型同内容」快速定位，供去重/溯源查询。
        Index("ix_asset_service_asset_versions_type_sha256", "asset_type", "sha256"),
        {"schema": SCHEMA_NAME},
    )

    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
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
    intent_match_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    human_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
