"""ORM 层公共实体信封 mixin（C.3.1 的持久化单一事实源，跨模块共享）。

为什么上提到 packages/shared：
- 资产域（asset_service）等新模块同样需要 C.3.1 信封列。若继续从
  project_planning 反向导入，模块间依赖方向就被破坏（资产域依赖规划域）。
- 信封列是**同一套映射机制**，只能有一份实现，否则字段长度/类型会漂移。

业务列仍由各模块自己的模型声明；本 mixin 只负责信封与 tenant_id。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）。
SCHEMA_VERSION = 205


class EntityMixin:
    """C.3.1 通用实体信封列 + 平台级 tenant_id（供各领域 ORM 模型继承）。

    注意：本 mixin 不含 __tablename__ / __table_args__，由具体模型声明，
    避免与 SQLAlchemy 声明式基类的 schema 配置冲突。

    tenant_id（决策 5）：多租户行级隔离键，Uuid 类型，配合 PostgreSQL RLS
    策略 ``USING (tenant_id = current_setting('app.current_tenant_id')::uuid)``
    在数据库层按死越权。
    """

    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_module: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audit_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
