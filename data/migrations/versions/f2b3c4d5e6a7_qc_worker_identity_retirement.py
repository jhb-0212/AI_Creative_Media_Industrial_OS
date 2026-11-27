"""退役 ``qc_worker``：它是空壳登录身份，Worker 真正连库用的是 ``qc_governance_role``。

为什么退役（不是「补授权」）
--------------------------
``AUTHZ-5`` 记的是「qc_worker 无非超级用户业务授权」。复核后这个提法本身就不准确：

- QC Worker 进程（``workers/qc``）建的是**业务域池**——``qc_governance_role`` 读任务、
  ``asset_service_role`` 读资产与写报告（见 ``workers/qc/__main__.py`` 的 pool 装配），
  租户上下文由 ``set_tenant_context`` 事务级注入；
- ``qc_worker`` 这个登录角色在整个仓库里**没有任何连接点**：没有 DSN 构造器、
  没有进程入口、没有池工厂；集群里也零依赖、零成员关系（``pg_shdepend`` 为空）。

所以它既不是「Worker 的身份」，也不是「待补权限的运行角色」，而是 WP-03 设计里
留下的反例壳身份。给它补授权，只会凭空造出一套无人使用、却可登录的权限面——
这正是「不登记的集群角色是审计看不到的权限面」那条要防的东西。

为什么这不会削弱「Worker 不能维护」的边界
----------------------------------------
那条边界真正的判据是「Worker 实际使用的身份不能过维护守卫」。本迁移之后由
``tests/integration/test_qc_maintenance_identities_pg.py`` 直接断言
``qc_governance_role`` 不是 operator 成员、没有守卫 EXECUTE——比对一个不存在的
角色更难被绕过：以前只要 Worker 换一个身份连库就失真了。

集群级 DROP 的守卫
------------------
``DROP ROLE`` 是集群操作。这里有依赖就**显式失败**而不是静默跳过：跳过会让
「退役了」这句话变成假话，而迁移报告仍是绿的。存在依赖时错误信息给出条数，
由部署方按 ``scripts/retire_legacy_roles.py`` 的口径先清引用。

downgrade 重建登录身份时不再手写属性与口令：改由
``migration_identity.restore_retired_role`` 按 `role_specs.RETIRED_ROLES`
的 `historical_attributes` 恢复，并要求受控入口显式确认
（``AUTHORIZE_RETIRED_ROLE_RESTORE=f2b3c4d5e6a7``）；凭据缺失或未授权即失败关闭
（WP-03 方案 A 的降级恢复契约）。
"""

from __future__ import annotations

from alembic import op

from packages.shared.db_roles import maintenance_role
from packages.shared.migration_identity import restore_retired_role

revision: str = "f2b3c4d5e6a7"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels: str | None = None
depends_on: str | None = None


def _worker() -> str:
    return maintenance_role("worker")


def upgrade() -> None:
    role = _worker()
    # 依赖检查放在同一条 DO 块里，与 DROP 同事务：先看有没有引用，有就带条数
    # 报错；没有才删。分两步做（先查再删）在两语句之间给人留了插空子的窗口。
    op.execute(
        f"""
        DO $$
        DECLARE
            dependency_count integer;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                RETURN;
            END IF;
            SELECT count(*) INTO dependency_count
              FROM pg_shdepend d
              JOIN pg_roles r ON r.oid = d.refobjid
             WHERE d.refclassid = 'pg_authid'::regclass
               AND r.rolname = '{role}';
            IF dependency_count > 0 THEN
                RAISE EXCEPTION
                    '角色 % 仍有 % 条集群级依赖，拒绝 DROP：先按 '
                    'scripts/retire_legacy_roles.py 的口径清理引用',
                    '{role}', dependency_count
                    USING ERRCODE = 'dependent_objects_still_exist';
            END IF;
            EXECUTE format('DROP ROLE %I', '{role}');
        END $$;
        """
    )


def downgrade() -> None:
    # 属性、成员关系、口令字段名全部来自 ``role_specs.RETIRED_ROLES`` 的历史定义；
    # 未授权（``AUTHORIZE_RETIRED_ROLE_RESTORE=<revision>``）或凭据缺失时失败关闭。
    # 恢复自带 ``RAISE WARNING``：重建集群级登录身份不允许只留一条日志。
    restore_retired_role(_worker(), op=op)
