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

from packages.shared.db_roles import app_role_name
from packages.shared.migration_identity import assert_role_present

# revision identifiers, used by Alembic.
revision: str = "9e3b4c5d6e7f"
down_revision: str | Sequence[str] | None = "8f2a1b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMAS = ("project_planning", "outbox")


def _app_password() -> str:
    """应用角色口令的唯一来源：settings（环境变量 / 密钥管理）。

    口令不出现在版本库中；缺失直接失败，避免以空口令或开发口令建角色。
    单引号按 SQL 字面量规则转义（``'`` → ``''``）。

    迁移经裸 ``get_settings()`` 读配置、不声明进程身份，进程层的生产
    校验不会触发——必须在此显式拒绝开发默认口令（与 relay/域角色迁移
    同一守卫，见 ``reject_development_default_password``）。
    """
    from packages.shared.config import (
        get_settings,
        reject_development_default_password,
    )

    settings = get_settings()
    password = settings.postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建应用角色"
        )
    reject_development_default_password(
        settings, "postgres_app_password", "POSTGRES_APP_PASSWORD"
    )
    return password.replace("'", "''")


def upgrade() -> None:
    """断言应用角色存在（由 bootstrap 创建）并授予最小权限（NOBYPASSRLS 使 RLS 生效）。"""
    # 1. 角色归 bootstrap：只断言存在，不建角色、不改属性、不读口令（WP-02）。
    #    原写法把角色属性与口令变成迁移的第二份事实源，并迫使 downgrade 删角色；
    #    `app_role` 是 9 个库共 36 条授权的落点，删它等于废掉所有库的连接配置。
    assert_role_present(app_role_name(), op=op)

    # 2. 授权 schema USAGE + 表/序列权限（最小权限）。
    for schema in _SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {app_role_name()}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {app_role_name()}")
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {app_role_name()}")

    # 3. 授权未来新建对象（默认权限，使后续迁移的表自动可访问）。
    for schema in _SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role_name()}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_role_name()}"
        )


def downgrade() -> None:
    """回滚：只撤销本迁移授出的授权，**不删除应用角色**（WP-02）。

    `app_role` 是集群级身份，真实集群上 9 个库共 36 条授权依赖它；迁移撤得掉
    自己授的那份，撤不掉其它迁移与其它库的。原 ``DROP ROLE`` 因此必然失败，
    且一旦在"单库"环境侥幸成功，就会连带废掉其它库的连接配置。
    """
    for schema in _SCHEMAS:
        # 默认权限必须显式撤：``REVOKE ALL ON ALL TABLES`` 只作用于**当前已存在**
        # 的表的 relacl，撤不掉 pg_default_acl 里那条"以后新建的表自动可读写"的
        # 规则。漏掉它，降级后 app_role 对该 schema 后续创建的一切表仍然自动持有
        # SELECT/INSERT/UPDATE/DELETE——这是权限泄漏，不是形态差异。
        # （实测：升级建出 2 个 schema × 2 类默认权限，旧降级在 D 态全部残留；
        #   补齐这两条 REVOKE 后 P==D，0 差异。）
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {app_role_name()}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {app_role_name()}"
        )
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {app_role_name()}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {app_role_name()}")
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {app_role_name()}")
