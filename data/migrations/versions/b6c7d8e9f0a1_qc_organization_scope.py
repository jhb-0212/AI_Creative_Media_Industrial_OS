"""QC 组织归属随事实传递（WP-D 的库层）。

为什么加这两列，而不是改事件契约
--------------------------------
``project_planning.projects`` 的 RLS 除租户外还要求
``organization_id = current_setting('app.current_organization_id')``；
``asset_service.asset_versions`` 经 ``EXISTS(projects)`` 继承、
``asset_service.qc_reports`` 再经 ``EXISTS(asset_versions)`` 传递。API 侧有
Principal，会话由 UoW 注入组织，一切正常；但 **QC Worker / 调度器 / 运维 CLI
都没有 Principal**——组织级项目的产物读回与报告核验在那里一律读不到，表现为
「资产不存在 / 报告不存在」，任务被反复领取、永不终结。

让组织进入 Worker 的路径有两条：

① 把 ``organization_id`` 加进 ``GENERATION_SUCCEEDED`` 载荷——那要改
   ``contracts/runtime/event_catalog.yaml``（``payload_schema_version: 1``、
   required 清单）并同步 ``packages/shared/event_payloads.py`` 与冻结基线，
   等于「先实现、后补契约」，且会让历史事件与新事件出现两套载荷；
② **从事件的来源表读组织**（本迁移）：``generation_tasks`` 是
   ``GENERATION_SUCCEEDED`` 的发出源，事件存在 ⟺ 终态已提交，所以那条行一定
   在；组织作为**已提交数据事实**从行里下传，事件契约一个字不动。

选 ② 的代价是两处新增列与一次回填，换来的是契约不变与「组织不靠推断」。

为什么 ``NULL`` 是有效值而不是缺失
--------------------------------
``NULL`` = 租户级项目（该项目的 ``organization_id`` 为空）。把它当作「缺失」
会让所有租户级任务退回「没有组织上下文 → 读不到资产」，与今天的租户级正常行为
刚好相反。因此列必须可空、且**不设**默认组织；``set_organization_context`` 传
``None`` 时会写成空串，策略按 ``NULL`` 处理，语义自洽。

为什么不顺带把组织写进 RLS 策略
------------------------------
``qc_tasks`` 的可见性当前是**租户级**（``tenant_scope_predicate``）：待检队列是
租户内共享的工作队列，不是组织私有数据。把组织加进策略会让同一租户下的调度器
必须逐组织枚举才能看到全队列，而调度器的职责恰恰是「系统级调度、租户级执行」。
本次只加**事实列**，不改可见性；将来若要收紧，必须另起一次带策略评审的批次。

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.rls import tenant_scope_predicate

revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRODUCTION_SCHEMA = "production_planning"
_GENERATION_TASKS = "generation_tasks"
_QC_SCHEMA = "qc_governance"
_QC_TASKS = "qc_tasks"
_MAINTENANCE_SCHEMA = "qc_maintenance"
_TASK_PROJECTION = "task_projection"


def _add_organization_id(schema: str, table: str) -> None:
    """幂等加列：可空 UUID，不建索引（不是过滤条件，是随行携带的事实）。"""
    op.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN IF NOT EXISTS organization_id UUID NULL")


def _policy_with_project_exists(schema: str, table: str) -> str:
    """旧谓词：租户 + ``EXISTS(projects)``（R16 分域时的形状）。

    保留它只为 ``downgrade`` 能还原到迁移前的状态；见下文为什么 ``upgrade``
    要去掉 ``EXISTS``。
    """
    return (
        f"USING ({tenant_scope_predicate()} AND "
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {schema}.{table}.project_id "
        f"AND p.tenant_id = {schema}.{table}.tenant_id))"
    )


def _recreate_policy(schema: str, table: str, using: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {schema}.{table}")
    op.execute(f"CREATE POLICY tenant_isolation_{table} ON {schema}.{table} {using}")


def _drop_projects_exists_from_policies() -> None:
    """去掉两条策略里的 ``EXISTS(projects)``，只剩租户谓词。

    为什么必须去掉
    --------------
    这条 ``EXISTS`` 让 ``qc_tasks`` / ``task_projection`` 的可见性**间接继承
    ``projects`` 的 RLS**（策略里的子查询以调用者身份执行，被引用表的策略照常
    生效）。于是「能不能看见待检任务」取决于「能不能看见那个项目」——而
    ``projects`` 的策略还要求组织上下文：组织级项目的任务对**调度器与 Worker
    一律不可见**。表现不是报错，而是空队列：调度发现 0 个租户、Worker 领不到
    任务、日志里什么都没有。这正是 WP-C 用 409 挡住的成因链，只是它发生在
    ``qc_tasks`` 而不是 ``asset_versions``。

    为什么这不削弱安全性
    --------------------
    这条 ``EXISTS`` 与租户复合外键**语义等价**（``c9d0e1f2a3b4`` 已把
    ``fk_qc_tasks_project_tenant`` / ``fk_task_projection_project_tenant``
    补成 ``(project_id, tenant_id) -> projects(entity_id, tenant_id)`` 并
    VALIDATE）。外键在**所有连接**上恒成立、不依赖 RLS 是否生效、违反时给的是
    ``23503`` 而不是 ``42501``——那才是归属事实该有的载体。该迁移自己的结论就是
    「策略替代不了外键」，本迁移只是把残留的那半边收回：归属交给外键，可见性
    留给租户谓词。

    也能反向证明：若外键缺失或未 VALIDATE，
    ``tests/integration/test_ownership_fk_constraints.py`` 会先报红——本迁移
    没有在无人看守处放宽任何东西。
    """
    _recreate_policy(_QC_SCHEMA, _QC_TASKS, f"USING ({tenant_scope_predicate()})")
    _recreate_policy(
        _MAINTENANCE_SCHEMA,
        _TASK_PROJECTION,
        f"USING ({tenant_scope_predicate()})",
    )


def _restore_projects_exists_policies() -> None:
    """``downgrade`` 还原旧谓词（含 ``EXISTS``），与升级前逐字一致。"""
    _recreate_policy(
        _QC_SCHEMA,
        _QC_TASKS,
        _policy_with_project_exists(_QC_SCHEMA, _QC_TASKS),
    )
    _recreate_policy(
        _MAINTENANCE_SCHEMA,
        _TASK_PROJECTION,
        _policy_with_project_exists(_MAINTENANCE_SCHEMA, _TASK_PROJECTION),
    )


def upgrade() -> None:
    # 生产域：生成任务落组织（创建时从验收签 Principal 取，见
    # ``GenerationService.request_generation``）。
    _add_organization_id(_PRODUCTION_SCHEMA, _GENERATION_TASKS)
    # QC 域：待检任务落组织（relay 从来源行读、人工恢复从事实端口读）。
    _add_organization_id(_QC_SCHEMA, _QC_TASKS)
    # 可见性收回租户维度：归属由复合外键守（见上面 docstring）。
    _drop_projects_exists_from_policies()


def downgrade() -> None:
    # 先把策略还原成「含 EXISTS」的旧形状，再删列：顺序反过来的话，还原后的
    # 策略在删列前的那一瞬与升级前并不一致，回滚的中间态就不可核验。
    _restore_projects_exists_policies()
    # 回滚**只**删列：组织这份事实没有别处存过，谈不上「还原」——删列后组织级
    # 项目的 QC 会退回到 Worker 侧 fail-closed（入口的 409 守卫已随之移除）。
    # 这是刻意的不对称：结构可逆，事实不可重建。
    op.execute(f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} DROP COLUMN IF EXISTS organization_id")
    op.execute(
        f"ALTER TABLE {_PRODUCTION_SCHEMA}.{_GENERATION_TASKS} "
        "DROP COLUMN IF EXISTS organization_id"
    )
