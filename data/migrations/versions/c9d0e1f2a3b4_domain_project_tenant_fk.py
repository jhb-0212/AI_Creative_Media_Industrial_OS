"""补齐 13 张域表的租户复合归属外键（D2 出证发现的归属缺口）

缺陷是什么
----------
正式链归属验收台账（``docs/audits/mig-recon/main-chain/ownership-ledger.md``，
D2 取证产物）对 15 张「含 project_id 列」的表逐张判定，结果 **FAIL，2/15 通过**。
两类缺口：

- **FAIL_NO_PATH（5 张）**：``budget_quota.shot_budget_requests`` /
  ``production_planning.generation_tasks`` /
  ``production_planning.production_routes`` /
  ``qc_governance.gate_decisions`` / ``qc_governance.qc_tasks``
  —— 对 ``projects`` **一条外键都没有**，``project_id`` 可以指向任意字符串。
- **GAP_TENANT_UNCOVERED（8 张）**：``episodes`` / ``rework_tasks`` /
  ``shot_breakdown_drafts`` 有单列 ``project_id -> projects.entity_id``；
  ``scenes`` / ``script_revisions`` / ``shot_revisions`` / ``shot_specs`` /
  ``shots`` 只经父链可达。两种情况都只保证「项目存在」或「与父行同一
  project_id」，**都不保证** 本行 ``tenant_id`` 等于该项目的租户。

根因不是「迁移写漏了一句」
--------------------------
R16 分域把这些表搬进 ``production_planning`` / ``budget_quota`` /
``qc_governance`` 时，归属判定被写成了 **RLS 策略的 USING 谓词**：

    USING (tenant_scope_predicate() AND
           EXISTS (SELECT 1 FROM project_planning.projects p
                   WHERE p.entity_id = qc_governance.qc_tasks.project_id
                     AND p.tenant_id = qc_governance.qc_tasks.tenant_id))

这条谓词与租户复合外键**语义等价**，所以「看起来已经管住了」。但两者的作用面
不同，策略替代不了外键：

1. **只在 RLS 生效的连接上有意义**。超级用户、迁移连接、``pg_dump`` 恢复，
   以及将来任何新角色，都不经过策略——外键则对所有连接恒成立。
2. **拒绝码不同**。违反策略是 ``42501``（看起来像「权限不足」），违反外键是
   ``23503``（``ReferentialIntegrityError``，可翻译成业务 4xx）。用策略守归属，
   会让「数据错」在调用方眼里变成「你没权限」。
3. **归属是否被检查取决于策略形态**。策略里若只写 ``USING``、且该表另有更宽的
   策略，判定可能根本没进 ``WITH CHECK``；更糟的是越权写会被静默过滤成
   ``UPDATE 0``——「没报错」被当成「改成功」。
4. **catalog 里查不到**。验收要的是 ``pg_constraint`` 上的 ``convalidated``
   事实，策略是 ``pg_policies`` 里的字符串表达式，无法被机器判定「约束已 VALIDATE」。

所以根治是两半：
- **本迁移**（修存量）：把 13 张表的归属约束补成与策略同语义的外键；
- **派生闸门**（防复发）：``tests/integration/test_ownership_fk_constraints.py``
  新增一条从 ``information_schema`` 派生目标集合的用例——「所有含 project_id
  的表都必须有覆盖 tenant_id 的 projects 外键且已 VALIDATE」。以后新增域表漏写
  约束会立刻报红，而不再需要有人记得来查台账。

为什么替换单列外键而不是并存
----------------------------
复合外键蕴含单列外键（前者成立时后者必成立）。两条约束守同一个事实就是两份
事实源，且每次写入多一次索引查找。已有 ``asset_versions`` /
``constraint_packs`` 就是「只有复合、没有单列」的形态，本迁移把 13 张表收敛到
同一形态。构造已收敛到 ``packages.shared.orm.project_tenant_fk``（约束名由表名
派生，避免 ``fk_shot_draft_project`` 那种「名字与表名不符」的漂移）。

为什么这里写死 13 张表
----------------------
迁移是**时间胶囊**：它记录「此刻哪 13 张表缺约束」。持续判定必须由派生闸门
承担——迁移里若写「遍历所有含 project_id 的表」，它在未来新增表的库上会做本
迁移当时没有声明的事，回滚语义也随之不成立。这与 ``b8c9d0e1f2a3`` 只写死三个
被修复域、而把 USAGE 集合改成派生函数是同一个分工。

NOT VALID → VALIDATE 的必要性
-----------------------------
本库实测脏行 = 0（15 张表孤儿 0、租户不一致 0），因此 ``VALIDATE`` 会立即通过。
仍然分两步写：``ADD CONSTRAINT ... NOT VALID`` 只取短暂的 ``ACCESS EXCLUSIVE``，
``VALIDATE CONSTRAINT`` 取 ``SHARE UPDATE EXCLUSIVE`` 且**不阻塞写入**。将来在
有脏行的库上执行时，``VALIDATE`` 会以真实的违规行失败——这就是「拒绝永久
豁免」的可机检形态，而不是靠文档声明。

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 归属根：目标键 uq_projects_entity_tenant (entity_id, tenant_id) 已存在，
# 这是本迁移能在不新建唯一键的前提下挂复合外键的原因。
_OWNERSHIP_ROOT = "project_planning.projects"

# (schema, table, 需先删除的旧单列外键名 | None)
#
# 三处 ``superseded`` 非空：单列外键被复合外键**替换**（理由见文档字符串）。
# 其余十处此前没有任何 projects 外键，纯新增。
_TARGETS: tuple[tuple[str, str, str | None], ...] = (
    # --- project_planning：内部父链完备，但父链不保证租户一致 ---
    ("project_planning", "episodes", "fk_episodes_project"),
    ("project_planning", "rework_tasks", "fk_rework_tasks_project"),
    ("project_planning", "shot_breakdown_drafts", "fk_shot_draft_project"),
    ("project_planning", "scenes", None),
    ("project_planning", "script_revisions", None),
    ("project_planning", "shot_revisions", None),
    ("project_planning", "shot_specs", None),
    ("project_planning", "shots", None),
    # --- R16 分域表：归属此前只存在于 RLS 策略里 ---
    ("budget_quota", "shot_budget_requests", None),
    ("production_planning", "generation_tasks", None),
    ("production_planning", "production_routes", None),
    ("qc_governance", "gate_decisions", None),
    ("qc_governance", "qc_tasks", None),
)


def _fk_name(table: str) -> str:
    """约束名由表名派生（与 packages.shared.orm.project_tenant_fk 同一口径）。"""
    return f"fk_{table}_project_tenant"


def upgrade() -> None:
    for schema, table, superseded in _TARGETS:
        qualified = f"{schema}.{table}"
        if superseded is not None:
            # 先删单列再建复合：若反序，中间态会有两条守同一事实的约束，
            # 虽然无害，但 downgrade 的「净变化」就不等于 upgrade 的逆。
            op.drop_constraint(superseded, table, schema=schema, type_="foreignkey")
        op.execute(
            f"""
            ALTER TABLE {qualified}
            ADD CONSTRAINT {_fk_name(table)}
            FOREIGN KEY (project_id, tenant_id)
            REFERENCES {_OWNERSHIP_ROOT} (entity_id, tenant_id)
            NOT VALID
            """
        )
        # 存量若违反约束，这里会以真实违规行失败——不吞异常、不降级为警告。
        op.execute(f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {_fk_name(table)}")


def downgrade() -> None:
    """撤回复合外键，并把被替换的三条单列外键原样恢复。

    恢复时必须同样 ``VALIDATE``：原来那三条的 ``convalidated`` 是 ``true``，
    若降级后留下一批未验证的外键，库的状态既不是升级前、也不是升级后，
    而是一个只在 catalog 上层面上「有约束」的混合态。
    """
    for schema, table, superseded in _TARGETS:
        op.drop_constraint(_fk_name(table), table, schema=schema, type_="foreignkey")
        if superseded is None:
            continue
        op.execute(
            f"""
            ALTER TABLE {schema}.{table}
            ADD CONSTRAINT {superseded}
            FOREIGN KEY (project_id)
            REFERENCES {_OWNERSHIP_ROOT} (entity_id)
            NOT VALID
            """
        )
        op.execute(f"ALTER TABLE {schema}.{table} VALIDATE CONSTRAINT {superseded}")
