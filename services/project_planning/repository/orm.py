"""Project ORM 模型（映射到独立 schema：project_planning）。

数据所有权（14.2）：
- project_planning 模块拥有 Project 表的写入权。
- 使用独立 schema「project_planning」，避免与其它模块耦合。
- 表结构严格对齐 C.3.1 通用实体信封字段。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "project_planning"


class ProjectModel(Base):
    """projects 表模型。"""

    __tablename__ = "projects"
    __table_args__ = {"schema": SCHEMA_NAME}

    # C.3.1 通用信封字段
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, default="Project")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_module: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audit_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 业务字段
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_tier: Mapped[str] = mapped_column(String(8), nullable=False)
