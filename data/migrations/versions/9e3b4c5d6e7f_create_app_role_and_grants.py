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

密码：app_role 的密码只走 settings（``POSTGRES_APP_PASSWORD``，生产由密钥
管理服务注入），迁移内不保留任何明文默认值；缺失即拒绝迁移。已存在角色
会用同一口令 ``ALTER ROLE``（幂等），避免「版本库里的开发口令」长期留存。

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

_SCHEMAS = ("project_planning", "outbox")


def _app_password() -> str:
    """应用角色口令的唯一来源：settings（环境变量 / 密钥管理）。

    口令不出现在版本库中；缺失直接失败，避免以空口令或开发口令建角色。
    单引号按 SQL 字面量规则转义（``'`` → ``''``）。
    """
    from packages.shared.config import get_settings

    password = get_settings().postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建应用角色"
        )
    return password.replace("'", "''")


def upgrade() -> None:
    """创建应用角色并授权（最小权限，NOBYPASSRLS 使 RLS 生效）。"""
    password = _app_password()

    # 1. 创建应用角色：LOGIN、非超级用户、NOBYPASSRLS（不绕过 RLS）。
    #    已存在时同步口令，避免历史开发口令留在角色上。
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{password}';
            ELSE
                ALTER ROLE {APP_ROLE} WITH PASSWORD '{password}';
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
