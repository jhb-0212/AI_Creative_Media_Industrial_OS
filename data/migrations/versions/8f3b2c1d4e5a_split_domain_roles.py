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

角色归身份治理（D4 裁决，Owner 2026-11-28）：
本迁移**不创建、不修改、不删除**基础角色，也不读口令。角色由
``scripts/bootstrap_identity.py`` 按 ``role_specs.py`` 先行创建与收敛属性；
迁移只用 ``assert_role_present`` 断言存在，缺失即 fail-closed 并提示先跑
Bootstrap。这样 upgrade/downgrade 的对称性不再取决于能否删掉集群级角色，
口令也不再进入迁移层。

Revision ID: 8f3b2c1d4e5a
Revises: 7a8b9c0d1e2f
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.migration_identity import assert_role_present

# revision identifiers, used by Alembic.
revision: str = "8f3b2c1d4e5a"
down_revision: str | Sequence[str] | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _assert_domain_role(role: str) -> None:
    """断言业务域角色已由 bootstrap 创建（角色归身份治理，不归迁移）。

    D4 裁决（Owner 2026-11-28）：基础角色的创建、属性、凭据与生命周期由
    Bootstrap Identity 统一负责；迁移只断言存在、管理自己的对象与授权。
    原实现在 upgrade 里 ``CREATE/ALTER ROLE`` + 写口令并强制重置属性——这既是
    第二份身份事实源，也让降级必须删角色才能对称，而角色是集群级对象。
    """
    assert_role_present(role, op=op)


def _grant_business_schema(role: str, schema: str) -> None:
    """授予业务 schema 的读写权限（USAGE + 表 DML + 序列 + 未来对象默认权限）。"""
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {role}"
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
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} TO {role}")


def _grant_auth_read(role: str) -> None:
    """授予 auth.revoked_tokens 只读（认证路径吊销检查）。"""
    op.execute(f'GRANT USAGE ON SCHEMA "{AUTH_SCHEMA}" TO {role}')
    op.execute(f'GRANT SELECT ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" TO {role}')


def _grant_revision_read(role: str) -> None:
    """授予 public.alembic_version 只读（/readyz 就绪探针读 schema revision）。"""
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def upgrade() -> None:
    """断言两个业务域角色存在，授各自最小权限，回收 app_role 越界权限。"""
    project_role, asset_role, app_role = _roles()

    # 1. 角色由 bootstrap 先行创建；缺失即 fail-closed 并提示修复动作。
    _assert_domain_role(project_role)
    _assert_domain_role(asset_role)

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
    """回滚：恢复 app_role 越界权限，撤销两个业务域角色的授权（**不删角色**）。

    可逆性口径（审查 P2-02 + WP-02 裁决 F3）：
    - 先**恢复** app_role 的权限（回到拆角色前的授权面）；
    - 再逐个业务域角色 **REVOKE 全部授权 + 撤销默认权限**；
    - **不执行 DROP ROLE**：角色是集群级对象，迁移只拥有"自己这条链上的对象与
      授权"，不拥有基础角色的删除权（`role_specs.py` 是身份生命周期唯一事实源）。
      原实现删除角色会在共享集群里失败，且让本库迁移结果受其他库状态影响。

    降级后的目标态是"角色存在、在本迁移管理的授权范围内零授权"，
    不是"整个集群零授权"——跨库依赖既不在本迁移的可撤销范围，也不得被擅自清理。
    """
    project_role, asset_role, app_role = _roles()

    # 恢复 app_role 的权限：必须**逐条**还原父态的实际授权面，不能按
    # "ALL TABLES IN SCHEMA" 一把授。
    # 父态（7a8b9c0d1e2f）逐表实测（has_table_privilege，非 relacl 猜测）：
    #   asset_service.asset_versions : INSERT, SELECT, UPDATE
    #   asset_service.qc_reports     : INSERT, SELECT
    #   project_planning.episodes / projects / script_revisions : DELETE, INSERT, SELECT, UPDATE
    #   auth.revoked_tokens          : SELECT
    #   public.alembic_version       : SELECT
    #   序列：无；organization 表：无 app_role 授权
    # 旧实现给两 schema 的"所有表"授 SELECT/INSERT/UPDATE/DELETE，D 会比 P
    # 多出 app_role 在 organizations、asset 其他表的写权限——这是降级态越权。
    for schema in ("project_planning", "asset_service", AUTH_SCHEMA):
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {app_role}")
    for table, privileges in (
        ("asset_service.asset_versions", "INSERT, SELECT, UPDATE"),
        ("asset_service.qc_reports", "INSERT, SELECT"),
        ("project_planning.episodes", "DELETE, INSERT, SELECT, UPDATE"),
        ("project_planning.projects", "DELETE, INSERT, SELECT, UPDATE"),
        ("project_planning.script_revisions", "DELETE, INSERT, SELECT, UPDATE"),
        (f"{AUTH_SCHEMA}.{AUTH_TABLE}", "SELECT"),
        (ALEMBIC_VERSION, "SELECT"),
    ):
        op.execute(f"GRANT {privileges} ON TABLE {table} TO {app_role}")

    # 撤销两个业务域角色在本迁移授权范围内的全部授权与默认权限。
    for role, schema in (
        (project_role, "project_planning"),
        (asset_role, "asset_service"),
    ):
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}")
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {role}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON SEQUENCES FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {role}")
    # WP-04 D-08 归因补充：upgrade 是"schema × 角色"全量授予，降级必须按同一张
    # 授权面对称撤销，而不是按"角色↔自家 schema"配对——交叉默认权限与 public
    # schema 的授权正是旧实现漏掉、导致本库残留依赖的三条。
    for role, cross_schema in (
        (project_role, "asset_service"),
        (asset_role, "project_planning"),
    ):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {cross_schema} REVOKE ALL ON TABLES FROM {role}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {cross_schema} REVOKE ALL ON SEQUENCES FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
    # 共享基础设施与就绪探针的授权也要撤（本库范围内必须收干净）。
    for role in (project_role, asset_role):
        op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {OUTBOX_SCHEMA} FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA {OUTBOX_SCHEMA} FROM {role}")
        op.execute(f'REVOKE ALL ON TABLE "{AUTH_SCHEMA}"."{AUTH_TABLE}" FROM {role}')
        op.execute(f'REVOKE USAGE ON SCHEMA "{AUTH_SCHEMA}" FROM {role}')
        op.execute(f"REVOKE SELECT ON {ALEMBIC_VERSION} FROM {role}")
