"""按业务域拆分数据库角色：project_planning_role / asset_service_role（DATA-1）

根因（F03 数据所有权：模块级角色隔离，非补丁）：
- 单一 ``app_role`` 被授予 project_planning / asset_service / outbox / auth
  四个 schema 的权限，project_planning 与 asset_service 两个业务域共用同一
  角色与同一连接池。数据库层无法阻止「project_planning 域代码越界写
  asset_service 表」（或反之）——一旦某域代码被攻破或误改，就能直写他域数据。
- 根治：为每个业务域建独立数据库角色（LOGIN、NOSUPERUSER、NOBYPASSRLS），
  业务 schema 写权限严格隔离：
    - project_planning_role：仅写 project_planning schema；
    - asset_service_role：仅写 asset_service schema。
  共享基础设施（跨域同事务投递的事件总线 outbox、认证吊销表 auth）仍由
  两角色按需持有——outbox 是平台级事件总线（见 packages/shared/outbox.py），
  auth.revoked_tokens 是认证路径只读依赖（见迁移 c6d7e8f9a0b1）。

角色设计（与 DATA-3 的 outbox_relay_role 同构）：
- 各角色：NOBYPASSRLS、LOGIN、NOCREATEDB/NOCREATEROLE/NOSUPERUSER（最小权限）。
- 授权范围：
    - project_planning_role：project_planning（USAGE + 表读写 + 序列）
      + outbox（USAGE + 表写 + 序列，同事务事件）
      + auth.revoked_tokens（SELECT，认证路径只读）。
    - asset_service_role：asset_service（USAGE + 表读写 + 序列）
      + outbox（USAGE + 表写 + 序列）
      + auth.revoked_tokens（SELECT）。
- 收口：回收 app_role 的越界权限，使旧角色不再被任何业务域使用（新域
  连接改用 project_planning_role / asset_service_role，见 database.py）。

密码：各角色密码只走 settings（POSTGRES_PROJECT_PLANNING_PASSWORD /
POSTGRES_ASSET_SERVICE_PASSWORD，生产由密钥管理服务注入），迁移内不保留
明文默认值；缺失即拒绝迁移。已存在角色用同一口令 ALTER ROLE（幂等）。

Revision ID: 8f3b2c1d4e5a
Revises: 7a8b9c0d1e2f
Create Date: 2026-11-18
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3b2c1d4e5a"
down_revision: str | Sequence[str] | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT_PLANNING_ROLE = "project_planning_role"
ASSET_SERVICE_ROLE = "asset_service_role"
APP_ROLE = "app_role"

OUTBOX_SCHEMA = "outbox"
AUTH_SCHEMA = "auth"
AUTH_TABLE = "revoked_tokens"
# 就绪探针（/readyz）需读 schema revision；与迁移 b7c8d9e0f1a2 给 app_role
# 授的 public.alembic_version 只读元数据一致，拆角色后转授两个业务域角色。
ALEMBIC_VERSION = "public.alembic_version"


def _roles() -> tuple[str, str, str]:
    """返回 (project_planning, asset_service, app) 三个角色名，唯一来源是配置。

    审查 R6：角色名在配置里可改（``POSTGRES_*_USER``），迁移若硬编码角色名，
    部署时改名就会「迁移建了 A 角色、进程连 B 角色」——权限边界与运行时角色
    脱节。三个名字与 DSN 构造（packages/shared/database.py）同源。
    """
    from packages.shared.config import get_settings

    settings = get_settings()
    return (
        settings.postgres_project_planning_user,
        settings.postgres_asset_service_user,
        settings.postgres_app_user,
    )


def _domain_password(field: str, env_name: str) -> str:
    """业务域角色口令的唯一来源：settings（环境变量 / 密钥管理）。

    口令不出现在版本库中；缺失直接失败，避免以空口令或开发口令建角色。
    单引号按 SQL 字面量规则转义（``'`` → ``''``）。

    迁移经裸 ``get_settings()`` 读配置、不声明进程身份，进程层的生产
    校验不会触发——必须在此显式拒绝开发默认口令（Codex P1：否则生产
    迁移未注入口令时会以仓库里可预测的 *_dev_password 建域角色）。
    """
    from packages.shared.config import (
        get_settings,
        reject_development_default_password,
    )

    settings = get_settings()
    password = getattr(settings, field).get_secret_value()
    if not password:
        raise RuntimeError(
            f"{env_name} 未配置：拒绝以空口令创建业务域角色"
        )
    reject_development_default_password(settings, field, env_name)
    return password.replace("'", "''")


def _create_role_if_absent(role: str, password: str) -> None:
    """创建业务域角色（幂等；已存在则同步口令**并重置属性**）。

    属性重置（Codex P1）：存量角色若已带 SUPERUSER/BYPASSRLS/NOSUPERUSER
    等危险属性，只同步口令会原样保留——角色必须强制收敛到最小权限集
    （LOGIN + 四个 NO*），与新建路径完全一致。
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{password}';
            ELSE
                ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOBYPASSRLS PASSWORD '{password}';
            END IF;
        END
        $$
        """
    )


def _grant_business_schema(role: str, schema: str) -> None:
    """授予业务 schema 的读写权限（USAGE + 表 DML + 序列 + 未来对象默认权限）。"""
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} "
        f"TO {role}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
    )


def _revoke_append_only_overreach(role: str) -> None:
    """收回业务域角色对 append-only 表的越权 DML（继承 b7c8d9e0f1a2 的授权基线）。

    根因：b7c8d9e0f1a2 已建立 append-only 授权口径——``qc_reports`` 只允许
    INSERT/SELECT（撤销 UPDATE/DELETE）、``asset_versions`` 撤销 DELETE
    （证据与版本表不接受应用的整行删改）。DATA-1 拆角色时若只做
    ``GRANT ... ON ALL TABLES IN SCHEMA`` 全量 DML，会把这两个收紧一并抹掉，
    使新角色重新获得越权删改能力（授权越界回归）。因此授完 schema 全量
    DML 后，必须对 append-only 表**额外收回越权 DML**，使域角色口径与
    app_role 旧口径完全对齐，不因拆角色而放大授权面。
    """
    op.execute(f"REVOKE UPDATE, DELETE ON asset_service.qc_reports FROM {role}")
    op.execute(f"REVOKE DELETE ON asset_service.asset_versions FROM {role}")


def _grant_outbox_write(role: str) -> None:
    """授予 outbox 事件总线写权限（跨域同事务投递，INSERT + 序列）。"""
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(
        f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}"
    )
    op.execute(
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} TO {role}"
    )


def _grant_auth_read(role: str) -> None:
    """授予 auth.revoked_tokens 只读（认证路径吊销检查）。"""
    op.execute(f'GRANT USAGE ON SCHEMA "{AUTH_SCHEMA}" TO {role}')
    op.execute(
        f'GRANT SELECT ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" TO {role}'
    )


def _grant_revision_read(role: str) -> None:
    """授予 public.alembic_version 只读（/readyz 就绪探针读 schema revision）。"""
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def upgrade() -> None:
    """创建两个业务域角色并授各自最小权限，回收 app_role 越界权限。"""
    project_password = _domain_password(
        "postgres_project_planning_password", "POSTGRES_PROJECT_PLANNING_PASSWORD"
    )
    asset_password = _domain_password(
        "postgres_asset_service_password", "POSTGRES_ASSET_SERVICE_PASSWORD"
    )
    project_role, asset_role, app_role = _roles()

    # 1. 创建业务域角色（幂等）。
    _create_role_if_absent(project_role, project_password)
    _create_role_if_absent(asset_role, asset_password)

    # 2. 授予各业务域只写自己的 schema。
    _grant_business_schema(project_role, "project_planning")
    _grant_business_schema(asset_role, "asset_service")

    # 2b. 收回 asset_role 对 append-only 表的越权 DML（继承
    #     b7c8d9e0f1a2 的授权基线：qc_reports 不可 UPDATE/DELETE、
    #     asset_versions 不可 DELETE），不因拆角色放大授权面。
    _revoke_append_only_overreach(asset_role)

    # 3. 共享基础设施：outbox 事件总线（同事务投递）与 auth 吊销表（只读）。
    _grant_outbox_write(project_role)
    _grant_outbox_write(asset_role)
    _grant_auth_read(project_role)
    _grant_auth_read(asset_role)

    # 4. 就绪探针元数据：两角色均需读 public.alembic_version（/readyz）。
    _grant_revision_read(project_role)
    _grant_revision_read(asset_role)

    # 5. 收口：回收 app_role 的越界权限。app_role 不再被业务域使用
    #    （新连接池改用 project_planning_role / asset_service_role），
    #    移除其对 asset_service / auth / project_planning 的权限，避免旧角色
    #    残留越界授权面，彻底终结单一角色跨域读写。
    #
    #    注意：outbox 的写权限在此**不重复回收**（本迁移只收自己授出的边界），
    #    由迁移 c1f2e3d4a5b6 专门收口 app_role 的 outbox 写权限（审查 P1-02），
    #    使「谁收回哪块」各自有据、降级时不会互相踩。
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA asset_service FROM {app_role}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA asset_service FROM {app_role}")
    op.execute(f"REVOKE USAGE ON SCHEMA asset_service FROM {app_role}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA project_planning FROM {app_role}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA project_planning FROM {app_role}")
    op.execute(f"REVOKE USAGE ON SCHEMA project_planning FROM {app_role}")
    op.execute(f'REVOKE ALL ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" FROM {app_role}')
    op.execute(f'REVOKE USAGE ON SCHEMA "{AUTH_SCHEMA}" FROM {app_role}')
    op.execute(f"REVOKE SELECT ON {ALEMBIC_VERSION} FROM {app_role}")


def downgrade() -> None:
    """回滚：恢复 app_role 越界权限，删除两个业务域角色。

    可逆性口径（审查 P2-02）：
    - 先**恢复** app_role 的权限（回到拆角色前的授权面）；
    - 再逐个业务域角色 **REVOKE 全部授权 + 撤销默认权限**，最后才 DROP ROLE。
      PG 拒绝删除仍持有对象权限/默认权限的角色；不先撤销就 DROP，降级会失败，
      ``upgrade → downgrade → upgrade`` 无法往返。
    """
    project_role, asset_role, app_role = _roles()

    # 恢复 app_role 对 project_planning / asset_service / auth / revision 的权限。
    for schema in ("project_planning", "asset_service"):
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {app_role}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
            f"{schema} TO {app_role}"
        )
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {app_role}")
    op.execute(f'GRANT USAGE ON SCHEMA "{AUTH_SCHEMA}" TO {app_role}')
    op.execute(
        f'GRANT SELECT ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" TO {app_role}'
    )
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {app_role}")

    # 删除两个业务域角色：先撤销全部授权与默认权限，再 DROP。
    for role, schema in (
        (project_role, "project_planning"),
        (asset_role, "asset_service"),
    ):
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {role}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {role}")
    # 共享基础设施与就绪探针的授权也要撤（否则 DROP ROLE 仍会因依赖失败）。
    for role in (project_role, asset_role):
        op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
        op.execute(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {OUTBOX_SCHEMA} FROM {role}")
        op.execute(
            f'REVOKE ALL ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" FROM {role}'
        )
        op.execute(f'REVOKE USAGE ON SCHEMA "{AUTH_SCHEMA}" FROM {role}')
        op.execute(f"REVOKE SELECT ON {ALEMBIC_VERSION} FROM {role}")
        op.execute(f"DROP ROLE IF EXISTS {role}")
