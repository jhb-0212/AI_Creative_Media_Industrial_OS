"""auth.revoked_tokens 的 ORM 映射（仅供迁移 autogenerate 对账）。

应用运行时只读（app_role 仅 SELECT），写入走运维 CLI 的迁移角色；
本文件不提供仓储层，避免把"吊销"变成业务可调用的常规写路径。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base

SCHEMA_NAME = "auth"


class RevokedTokenModel(Base):
    """auth.revoked_tokens（jti denylist，P3-2 / 迁移 c6d7e8f9a0b1）。"""

    __tablename__ = "revoked_tokens"
    __table_args__ = (
        Index("ix_auth_revoked_tokens_expires_at", "expires_at"),
        {"schema": SCHEMA_NAME},
    )

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revoked_by: Mapped[str] = mapped_column(String(64), nullable=False)
