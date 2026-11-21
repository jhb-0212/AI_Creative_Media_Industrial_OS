"""批次九：落地 `workflow.rework_tasks`，使返工依据引用不再是悬空边。

为什么必须建表而不是改判据
--------------------------
`shot_planning.shots.rework_basis_ref` 是一条真实的对象引用（契约
`Shot.rework_basis_ref → ReworkTask`），不是「看起来像外键的展示字段」。
`verify_ownership_consistency` 把它登记为 DANGLING 而不是 NonRef，正是拒绝
把「目标未实施」永久洗白。把判据改成「不检查」只会让下一次真正的悬空引用
也一起消失——那是本项目反复踩过的同一种坑。

表结构来自哪里
---------------
字段/状态取自 `contracts/schemas/object_schemas.schema.json::ReworkTask` 与
`contracts/state_machines/state_machines.yaml::ReworkTask`（六态、四条边；
REJECTED 合法但无授权入边，属于已登记的 transitions_gap，不在本迁移里发明边）。
归属三列 + RLS 口径取自同 schema 的 `approval_tickets`，不新造第二种模式。

身份单一：``entity_id = rework_task_id`` 由 CHECK 约束守，不由应用自觉——
与 `scenes` 的 ``entity_id = scene_id`` 同一条铁律。

外键落地顺序（ADR-R40 归属铁律）
--------------------------------
先建目标键 → 再挂 NOT VALID 外键 → 存量订正 → VALIDATE → 台账全 PASS。
NOT VALID 只豁免历史行、新写入照样检查；`shots` 当前所有
`rework_basis_ref` 都是 NULL，VALIDATE 必然通过，但这一步不能省——省了就
无法证明「存量真的没有违例」，只能靠「我知道没有」。

Revision ID: m3b4c5d6e7f8
Revises: l2a3b4c5d6e7
"""

from __future__ import annotations

from alembic import op

revision = "m3b4c5d6e7f8"
down_revision = "l2a3b4c5d6e7"
branch_labels = None
depends_on = None

TABLE = "workflow.rework_tasks"
SHOTS = "shot_planning.shots"
FK = "fk_shots_rework_basis"

_CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    entity_id                  varchar      NOT NULL,
    entity_type                varchar      NOT NULL,
    schema_version             integer      NOT NULL,
    entity_version             integer      NOT NULL,
    status                     varchar      NOT NULL,
    owner_module               varchar      NOT NULL,
    created_at                 timestamptz  NOT NULL,
    created_by                 varchar      NOT NULL,
    updated_at                 timestamptz  NOT NULL,
    audit_ref                  varchar,
    tenant_id                  uuid         NOT NULL,
    organization_id            uuid         NOT NULL,
    project_id                 varchar      NOT NULL,
    rework_task_id             varchar      NOT NULL,
    issue_id                   varchar      NOT NULL,
    defect_category            varchar      NOT NULL,
    root_cause                 text         NOT NULL,
    severity                   varchar      NOT NULL,
    return_node                varchar      NOT NULL,
    target_ref                 varchar      NOT NULL,
    owner                      varchar      NOT NULL,
    constraint_pack_ref        varchar,
    downstream_task_ids        jsonb        NOT NULL DEFAULT '[]'::jsonb,
    affected_asset_version_ids jsonb        NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT rework_tasks_pkey PRIMARY KEY (entity_id),
    CONSTRAINT uq_rework_tasks_identity UNIQUE (rework_task_id),
    -- 复合外键的落地键：返工授权不能跨项目借用。
    -- 只有全局唯一的 rework_task_id 时，A 项目可以拿 B 项目的返工单号填进来；
    -- (project_id, rework_task_id) 才与 shots 的归属列形成同一条链。
    CONSTRAINT uq_rework_tasks_project_id UNIQUE (project_id, rework_task_id),
    CONSTRAINT ck_rework_tasks_identity CHECK (entity_id = rework_task_id),
    CONSTRAINT ck_rework_tasks_status CHECK (
        status IN ('OPEN', 'IN_PROGRESS', 'DONE', 'VERIFYING', 'CLOSED', 'REJECTED')
    ),
    CONSTRAINT fk_rework_tasks_project_tenant_org
        FOREIGN KEY (project_id, tenant_id, organization_id)
        REFERENCES project_planning.projects (entity_id, tenant_id, organization_id)
)
"""

# 与 approval_tickets 完全同构的 RLS：租户、组织+项目、隔离项目、受控修复、
# 审计隔离区五个维度。表名/身份列换成本表，判定语义一个字不改——
# 同域两种口径会让「同样的归属在两张表里可见性不同」，那是最难查的一类缺口。
_GRANT_RLS = f"""
CREATE POLICY tenant_isolation_rework_tasks ON {TABLE}
    FOR ALL TO PUBLIC
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
"""
_PROJECT_RLS = f"""
CREATE POLICY project_scope_rework_tasks ON {TABLE}
    FOR ALL TO PUBLIC
    USING (
        (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
         AND (current_setting('app.current_projects', true) = '*'
              OR project_id::text = ANY (string_to_array(
                  NULLIF(current_setting('app.current_projects', true), ''), ','))))
        OR project_planning.repair_row_allowed('workflow.rework_tasks', entity_id)
    )
    WITH CHECK (
        (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
         AND (current_setting('app.current_projects', true) = '*'
              OR project_id::text = ANY (string_to_array(
                  NULLIF(current_setting('app.current_projects', true), ''), ','))))
        OR project_planning.repair_row_allowed('workflow.rework_tasks', entity_id)
    )
"""
_QUARANTINE_RLS = f"""
CREATE POLICY quarantine_restrictive_rework_tasks ON {TABLE}
    AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (
        project_id IS NULL
        OR NOT project_planning.is_quarantine_project(project_id)
        OR pg_has_role(CURRENT_USER, 'afs_data_repair', 'member')
        OR current_setting('app.include_quarantine', true) = 'on'
    )
    WITH CHECK (
        project_id IS NULL
        OR NOT project_planning.is_quarantine_project(project_id)
        OR pg_has_role(CURRENT_USER, 'afs_data_repair', 'member')
        OR (current_setting('app.include_quarantine', true) = 'on'
            AND pg_has_role(CURRENT_USER, 'afs_audit', 'member'))
    )
"""
_REPAIR_RLS = f"""
CREATE POLICY repair_row_scope ON {TABLE}
    FOR ALL TO PUBLIC
    USING (
        current_setting('app.ownership_repair_ticket', true) IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM project_planning.ownership_repair_map m
            WHERE m.table_name = 'workflow.rework_tasks'
              AND m.row_key = entity_id::text
              AND m.ticket_ref = current_setting('app.ownership_repair_ticket', true)
              AND m.applied_at IS NULL
        )
    )
    WITH CHECK (
        current_setting('app.ownership_repair_ticket', true) IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM project_planning.ownership_repair_map m
            WHERE m.table_name = 'workflow.rework_tasks'
              AND m.row_key = entity_id::text
              AND m.ticket_ref = current_setting('app.ownership_repair_ticket', true)
              AND m.applied_at IS NULL
        )
    )
"""
_AUDIT_RLS = f"""
CREATE POLICY audit_quarantine_scope ON {TABLE}
    FOR ALL TO afs_audit
    USING (current_setting('app.include_quarantine', true) = 'on')
    WITH CHECK (current_setting('app.include_quarantine', true) = 'on')
"""

_UPGRADE_SQLS: tuple[str, ...] = (
    _CREATE_TABLE,
    f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY",
    _GRANT_RLS,
    _PROJECT_RLS,
    _QUARANTINE_RLS,
    _REPAIR_RLS,
    _AUDIT_RLS,
    # 目标键先立住，再挂**复合**外键；NOT VALID 只豁免历史行，新写入立刻受检。
    # 复合而非单列：跨项目借用别人的返工授权必须被数据库拒绝，不能靠应用层记得比 project。
    f"ALTER TABLE {SHOTS} ADD CONSTRAINT {FK} "
    f"FOREIGN KEY (project_id, rework_basis_ref) "
    f"REFERENCES {TABLE} (project_id, rework_task_id) NOT VALID",
    f"ALTER TABLE {SHOTS} VALIDATE CONSTRAINT {FK}",
)

_DOWNGRADE_SQLS: tuple[str, ...] = (
    f"ALTER TABLE {SHOTS} DROP CONSTRAINT IF EXISTS {FK}",
    f"DROP TABLE IF EXISTS {TABLE}",
)


def upgrade_steps() -> list[str]:
    """升级语句序列（唯一事实源，验证脚本只从这里取）。"""
    return list(_UPGRADE_SQLS)


def downgrade_steps() -> list[str]:
    """降级严格对称：先撤 shots 外键，再撤目标表。"""
    return list(_DOWNGRADE_SQLS)


def upgrade() -> None:
    for statement in upgrade_steps():
        op.execute(statement)


def downgrade() -> None:
    for statement in downgrade_steps():
        op.execute(statement)
