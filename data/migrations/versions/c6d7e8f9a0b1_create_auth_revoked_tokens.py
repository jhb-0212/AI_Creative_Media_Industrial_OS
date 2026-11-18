"""创建 auth.revoked_tokens（JWT jti 吊销表，P3-2）

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-11-14

为什么是 jti 而非 subject：吊销的粒度必须是"这一枚令牌"，否则吊销一个人的
一个设备会把他的全部会话打掉。`expires_at` 让表可随令牌自然过期清理，
不给 denylist 无限增长。

授权面：
- app_role 只拿 SELECT（认证路径只读）；
- INSERT 走迁移角色（运维 CLI scripts/revoke_token.py），应用进程无法
  给自己签发的令牌"撤"或"恢复"吊销记录；
- 无租户 RLS：jti 全局唯一，租户字段仅用于审计归属，不构成访问边界。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import app_role_name

revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "auth"
_TABLE = "revoked_tokens"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    op.create_table(
        _TABLE,
        sa.Column("jti", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=False),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_auth_revoked_tokens_expires_at",
        _TABLE,
        ["expires_at"],
        schema=_SCHEMA,
    )
    op.execute(f'GRANT USAGE ON SCHEMA "{_SCHEMA}" TO {app_role_name()}')
    op.execute(f'GRANT SELECT ON TABLE "{_SCHEMA}"."{_TABLE}" TO {app_role_name()}')


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS "{_SCHEMA}"."{_TABLE}"')
