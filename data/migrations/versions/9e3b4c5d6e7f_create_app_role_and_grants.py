"""create application role and grant schema access (RLS enforcement)

根因（P0-3 身份治理，RLS 生效前提）：
- 应用以 postgres 超级用户连接数据库，超级用户默认 BYPASSRLS，导致
  RLS 策略（tenant_isolation_*）形同虚设——「研发写 SELECT * 也只看到
  当前租户」的终极防呆实际不生效（R6 安全必须真实执行）。
- 根治：创建专用应用角色（LOGIN、非超级用户、NOBYPASSRLS），应用与
  测试均以该角色连接，使 RLS 真正执行。

角色设计：
- app_role：NOBYPASSRLS（不绕过 RLS）、LOGIN（可登录）、NOCREATEDB/
  NOCREATEROLE/NOSUPERUSER（最小权限）。
- 授权范围：project_planning / outbox 两个 schema 的 USAGE + 表读写权限
  + 序列权限（最小权限，不授予 DDL）。

密码：app_role 的密码由环境变量注入（生产走密钥管理服务）；本迁移
用固定占位符占位，实际密码由部署层 ALTER ROLE 设置。开发环境默认
密码见 .env.example（POSTGRES_APP_PASSWORD）。

Revision ID: 9e3b4c5d6e7f
Revises: 8f2a1b3c4d5e
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e3b4c5d6e7f"
down_revision: str | Sequence[str] | None = "8f2a1b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_role"
# 开发环境默认密码（生产由部署层 ALTER ROLE 覆盖；见 .env.example）。
# 注：此处占位密码仅用于本地开发，生产绝不复用。
_DEV_PASSWORD = "app_dev_password"

_SCHEMAS = ("project_planning", "outbox")


def upgrade() -> None:
    """创建应用角色并授权（最小权限，NOBYPASSRLS 使 RLS 生效）。"""
    # 1. 创建应用角色：LOGIN、非超级用户、NOBYPASSRLS（不绕过 RLS）。
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{_DEV_PASSWORD}';
            END IF;
        END
        $$
        """
    )

    # 2. 授权 schema USAGE + 表/序列权限（最小权限）。
    for schema in _SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {APP_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {APP_ROLE}")

    # 3. 授权未来新建对象（默认权限，使后续迁移的表自动可访问）。
    for schema in _SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
        )


def downgrade() -> None:
    """回滚：回收权限 + 删除应用角色。"""
    for schema in _SCHEMAS:
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {APP_ROLE}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {APP_ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
