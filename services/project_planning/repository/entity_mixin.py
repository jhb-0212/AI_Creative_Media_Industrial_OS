"""ORM 层公共实体信封 mixin（C.3.1 的持久化单一事实源）。

设计要点（消除重复机制 R4）：
- ProjectModel / ScriptModel / EpisodeModel 三个 ORM 模型重复声明了同一套
  C.3.1 通用信封列（entity_id/entity_type/schema_version/entity_version/
  status/owner_module/created_at/created_by/updated_at/audit_ref）。
- 这些是同一套持久化映射的多份复制，收敛到单一 mixin。各模型的业务列
  （project_name/project_tier/title 等）保留在各自类中。
- entity_version 是乐观锁唯一事实源（见 _versioning.conditional_update）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）。
SCHEMA_VERSION = 205


class EntityMixin:
    """C.3.1 通用实体信封列（供各领域 ORM 模型继承）。

    注意：本 mixin 不含 __tablename__ / __table_args__，由具体模型声明，
    避免与 SQLAlchemy 声明式基类的 schema 配置冲突。
    """

    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_module: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audit_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
