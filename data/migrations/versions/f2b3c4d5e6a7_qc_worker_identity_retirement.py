"""退役 ``qc_worker`` 的**登记边界**：本迁移不再执行任何身份生命周期操作。

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

职责迁出（D4 裁决，Owner 2026-11-28）
-----------------------------------
原实现把退役写成 ``upgrade`` 里的集群级角色销毁（依赖检查 + ``EXECUTE format``
动态执行），``downgrade`` 里再用 ``restore_retired_role`` 重建。
裁决否决了这种「迁移兼当身份管理器」的做法：

* 角色销毁的作用域是**集群**，不是本库——迁移一旦执行，就改动了本迁移对象之外
  的共享状态；矩阵也就永远只能在「删成功」与「整链失败」之间二选一；
* 退役/恢复的判据（依赖清查、成员关系、对象所有权、默认权限、凭据）属于
  **身份生命周期**，不属于结构迁移。

因此本 revision 现在只做两件事：**登记退役边界**（``role_specs.RETIRED_ROLES``
的 ``retired_at_revision`` 指向这里），以及**只读地提示**集群现状。真实退役由
``scripts/retire_legacy_roles.py --retired`` 执行：先全集群零引用才允许
``DROP ROLE``，有依赖就原样退出，必要时保留身份并限制登录等待处置。

两种状态必须分开：``RETIRED`` 只表示「已登记、普通 Bootstrap 不再创建」；
``RETIREMENT_EXECUTED`` 表示「真实集群中的退役操作已执行并通过验证」。前者是
声明事实，后者是执行事实，本迁移只负责前者。

``downgrade`` 刻意什么都不做：不为「回到父态」重新创建集群级登录身份。父态
是否仍有这个角色取决于历史部署，恢复它等于在降级路径里静默重建一个已退役身份。

Revision ID: f2b3c4d5e6a7
Revises: b6c7d8e9f0a1
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

from packages.shared.db_roles import maintenance_role
from packages.shared.role_specs import retired_roles_by_name

revision: str = "f2b3c4d5e6a7"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels: str | None = None
depends_on: str | None = None


def _worker() -> str:
    return maintenance_role("worker")


def upgrade() -> None:
    """登记退役边界；不建、不改、不删任何角色（退役归身份治理）。"""
    role = _worker()
    contract = retired_roles_by_name().get(role)
    if contract is None:  # 声明与迁移边界脱节时失败关闭，不给「悄悄不退役」留口子
        raise RuntimeError(f'role "{role}" 不在 RETIRED_ROLES：退役边界缺少声明来源，拒绝继续')
    role_literal = role.replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_literal}') THEN
                RAISE NOTICE '退役身份 % 仍存在于本集群：集群级销毁由身份治理执行 '
                    '（scripts/retire_legacy_roles.py --retired），迁移不销毁角色',
                    '{role_literal}';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # 什么都不做：不重建已退役的集群级登录身份，也不撤销不属本迁移的授权。
    # 父态是否仍有该角色取决于历史部署；「恢复」只能走受控的身份治理流程。
    return None
