"""批次五：把 `shot_id` / `scene_id` 从「裸字符串」变成「指向真实存在的对象」。

为什么这一批必须先建表、而不是先抱怨「修不完」
--------------------------------------------
前四批解决的是**已有父表**的归属：`projects` / `organizations` / `episodes` /
`asset_versions` 都在，外键有地方可挂。剩下 9 条不是同类问题——它们指向的三张表
在库里**根本不存在**：

    shot_planning.shots         ← 5 条边
    shot_planning.scenes        ← 3 条边
    qc_governance.gate_instances ← 1 条边

给不存在的表建外键，工程上无解。所以这不属于「归属约束没补齐」，而是
**镜头域与场次域的运行时从未落地**。本批次一次解决能够解决的那 8 条。

为什么 `gate_instances` 不建
---------------------------
`GateInstance` **没有任何 schema**。`contracts/phase0/contract_amendments.yaml`
（V205-R14-A8 的 residual_risks）原文：

    仍无 schema：后者被信封 audit_ref 在 82 个对象上引用，是下一个同型缺口

凭空建一张无契约的表，等于给系统造一个新的、无约束的事实源——这比留一条
已知缺口更坏。故本批次**不建**，闸门对它保留 `DANGLING` 并附可机检前提
（表不存在 ∧ 契约无定义），表一旦建起来该豁免立即失效。

分叉的真相：契约是分层的，不是混乱的
----------------------------------
此前把运行时 `episode_ref` / `scene_ref` 与契约 `episode_id` / `scene_id` 的差异
判为「既有分叉」。逐字段核对后结论不同：

    ShotBreakdownDraft（草案）契约里**就写** episode_ref，且可空——
        全剧拆解（breakdown_scope.level = PRODUCTION）时本来就没有单集；
    Shot / Scene / ShotRevision（正式）契约里写 episode_id / scene_id——
        它们是身份键，指向目标表的 entity_id。

所以草案层用业务编码、正式层用身份，是**契约刻意的分层**。唯一真正的偏差是
`shot_revisions` 把草案的 `_ref` 直接搬进了正式修订表——它同时丢了
`episode_id` 与 `scene_id` 两列。本批次补上，`_ref` 保留为来源追溯（草案从哪来），
但**不再是归属事实**。

外键形状不是本批次自创
--------------------
ADR-R19（`contract_amendments.yaml` V205-R14-A8）已记录所有者裁定：

    FOREIGN KEY (project_id, scene_id) REFERENCES scene(project_id, scene_id)
    UNIQUE (project_id, shot_code)

本批次照此执行，不另行设计。ADR-R19 同时登记「创建入口未定，须在 Scene 运行时
设计时一并裁定」——故本批次**只建结构、不建 service / action**，不替所有者
裁定「确认拆镜即建立正式场次」。

`Shot` 与首个 `ShotRevision` 的循环引用
--------------------------------------
契约要求两者在同一事务创建（ADR-R18）：`Shot.shot_revision_id` 指向首个修订，
`ShotRevision.shot_id` 指向镜头。两条都是 NOT NULL，故至少一条外键必须
`DEFERRABLE INITIALLY DEFERRED`，否则事务内先插的那条必然找不到对方。
本批次两条都设为可延迟：只设一条时，约束检查顺序会依赖 INSERT 顺序，
而「先建哪个」不是调用方应该关心的事。

顺序（不可倒）
--------------
1. `episodes` 补 `UNIQUE (entity_id, project_id)`——`scenes.episode_id` 的外键要有目标键；
   缺键时 `ADD FOREIGN KEY` 直接 42830，与数据脏无关（空表一样报）。
2. 建 `scenes`（含 RLS 与策略）。必须先于 `shots`：`shots.scene_id` 指向它。
3. `shot_revisions` 补 `episode_id` / `scene_id`，回填后设 NOT NULL。
4. 建 `shots`（含 RLS 与策略）。
5. `shot_revisions.shot_id → shots`：必须在 `shots` 之后。

本迁移**不解决**的
------------------
- `gate_instances`（无 schema，见上）。
- `shot_drafts.scene_ref`：`ShotBreakdownDraft` 契约的场次落在 `scene_breakdowns[]`
  数组内，没有顶层场次引用字段。故该列是**草案内部标识**，不是对象引用，
  不挂外键——闸门把它登记为可机检豁免（前提：契约确认该对象无顶层 scene 字段）。
- `shots` / `scenes` 的创建入口（action / service）：待所有者裁定。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "i9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "h8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
SHOT = "shot_planning"
GEN = "generation_execution"


class Step(NamedTuple):
    """一步一条语句。

    单语句限制的理由与批次四相同：多语句串在一次 ``execute`` 里时任一句失败
    整批回滚，无法判断到底哪一句失败。

    用 ``NamedTuple`` 而非 ``@dataclass``：Alembic 用 ``module_from_spec`` 加载
    迁移却不先登记 ``sys.modules``，dataclass 解析注解会拿到 ``None``。
    """

    sql: str


# ---------------------------------------------------------------------------
# 信封 10 列 + 归属 4 列：两张新表共用
# ---------------------------------------------------------------------------
#: 信封列。与既有归属表逐字一致——新表从第一天就用同一形状，
#: 否则「所有归属表都有这 14 列」这条不变量会在第一张新表上失效。
ENVELOPE_COLS = (
    "entity_id        varchar      NOT NULL",
    "entity_type      varchar      NOT NULL",
    "schema_version   integer      NOT NULL",
    "entity_version   integer      NOT NULL",
    "status           varchar      NOT NULL",
    "owner_module     varchar      NOT NULL",
    "created_at       timestamptz  NOT NULL",
    "created_by       varchar      NOT NULL",
    "updated_at       timestamptz  NOT NULL",
    "audit_ref        varchar",
)

#: 归属三列。`tenant_id` / `organization_id` 是 uuid——与 `projects` 一致，
#: 不是 varchar；写成 varchar 会让 `(project_id, tenant_id, organization_id)
#: → projects` 的外键直接 42804（类型不匹配）。
OWNERSHIP_COLS = (
    "tenant_id        uuid         NOT NULL",
    "organization_id  uuid         NOT NULL",
)


def _create_scenes_sql() -> str:
    cols = [
        *ENVELOPE_COLS,
        "scene_id         varchar      NOT NULL",
        "scene_code       varchar      NOT NULL",
        "episode_id       varchar      NOT NULL",
        "project_id       varchar      NOT NULL",
        *OWNERSHIP_COLS,
    ]
    return (
        f"CREATE TABLE {PLANNING}.scenes (\n  "
        + ",\n  ".join(cols)
        + ",\n  CONSTRAINT scenes_pkey PRIMARY KEY (entity_id)"
        + ",\n  CONSTRAINT uq_scenes_identity UNIQUE (scene_id)"
        # 信封主键与业务身份必须同值（契约 C.3.1：不得各自生成两套标识）。
        # 只靠应用层赋值时，一次绕过就永久产生两个身份，且没有任何查询会报错。
        + ",\n  CONSTRAINT ck_scenes_single_identity CHECK (entity_id = scene_id)"
        + ",\n  CONSTRAINT uq_scenes_project_scene UNIQUE (project_id, scene_id)"
        + ",\n  CONSTRAINT uq_scenes_project_code UNIQUE (project_id, scene_code)"
        # 三态镜像 Episode（ADR-R19，source_kind: derived_from_parent_lifecycle）。
        # 收枚举而非裸 string：裸 string 等于任何值都接受，未知状态无法被拒绝。
        + ",\n  CONSTRAINT ck_scenes_status CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED'))"
        + f",\n  CONSTRAINT fk_scenes_project_tenant_org FOREIGN KEY (project_id, tenant_id, organization_id)"
        f" REFERENCES {PLANNING}.projects (entity_id, tenant_id, organization_id)"
        # 归属锚点：Scene 唯一的上行引用。带上 project_id 是为了让「同组织不同项目」
        # 的集无法被挂进来——单列 (episode_id) 只能证明集成存在。
         + f",\n  CONSTRAINT fk_scenes_episode_project FOREIGN KEY (episode_id, project_id)"
        f" REFERENCES {PLANNING}.episodes (entity_id, project_id)" + ")"
    )


def _create_shots_sql() -> str:
    cols = [
        *ENVELOPE_COLS,
        "shot_id          varchar      NOT NULL",
        "shot_code        varchar      NOT NULL",
        "scene_id         varchar      NOT NULL",
        "project_id       varchar      NOT NULL",
        "shot_revision_id varchar      NOT NULL",
        "blocked_reason   text",
        "rework_basis_ref varchar",
        "cancellation_ref varchar",
        "cancelled_at     timestamptz",
        "source_draft_ref jsonb",
        *OWNERSHIP_COLS,
    ]
    return (
        f"CREATE TABLE {SHOT}.shots (\n  "
        + ",\n  ".join(cols)
        + ",\n  CONSTRAINT shots_pkey PRIMARY KEY (entity_id)"
        + ",\n  CONSTRAINT uq_shots_identity UNIQUE (shot_id)"
        + ",\n  CONSTRAINT ck_shots_single_identity CHECK (entity_id = shot_id)"
        # ADR-R19 明列的纸面约束：UNIQUE (project_id, shot_code)
        + ",\n  CONSTRAINT uq_shots_project_code UNIQUE (project_id, shot_code)"
        # 指向镜头的边一律用它做目标键：单列 (shot_id) 只能证明「镜头存在」，
        # 证明不了「挂它的那一行在本项目」。前四批在别的域吃过这个教训——
        # `qc_reports` 就是挂在真实资产上、自己写另一个真实项目，三列外键一路放行。
        + ",\n  CONSTRAINT uq_shots_project_shot UNIQUE (project_id, shot_id)"
        # 五态（所有者 2026-11-18 裁定，ADR-R18）。刻意不含 IN_REVIEW / REWORK /
        # DELIVERED——审核归 GateInstance、返工归 ReworkTask、交付归 Gate 4。
        + ",\n  CONSTRAINT ck_shots_status CHECK (status IN "
        "('BLOCKED', 'CANCELLED', 'IN_PRODUCTION', 'NOT_STARTED', 'PRODUCTION_COMPLETE'))"
        # 双向约束，不是单向：「BLOCKED 时必填」单写时，非 BLOCKED 行仍可携带原因，
        # 于是「这一行为什么卡过」与「它现在卡不卡」变成两个可以互相矛盾的字段。
         + ",\n  CONSTRAINT ck_shots_blocked_reason CHECK ("
        "(status = 'BLOCKED' AND blocked_reason IS NOT NULL) OR "
        "(status <> 'BLOCKED' AND blocked_reason IS NULL))"
        + ",\n  CONSTRAINT ck_shots_cancelled CHECK ("
        "(status = 'CANCELLED' AND cancellation_ref IS NOT NULL AND cancelled_at IS NOT NULL) OR "
        "(status <> 'CANCELLED' AND cancellation_ref IS NULL AND cancelled_at IS NULL))"
        + f",\n  CONSTRAINT fk_shots_project_tenant_org FOREIGN KEY (project_id, tenant_id, organization_id)"
        f" REFERENCES {PLANNING}.projects (entity_id, tenant_id, organization_id)"
        # ADR-R19 裁定的形状：镜头归属经场次收敛到集，不由自身推导。
         + f",\n  CONSTRAINT fk_shots_scene_project FOREIGN KEY (project_id, scene_id)"
        f" REFERENCES {PLANNING}.scenes (project_id, scene_id)"
        # 与首个修订互为引用（ADR-R18：同一事务创建）。可延迟，否则事务内
        # 先插入的一方必然找不到对方——而「先建哪个」不该是调用方要操心的事。
         + f",\n  CONSTRAINT fk_shots_current_revision FOREIGN KEY"
        f" (shot_revision_id, project_id, tenant_id, organization_id)"
        f" REFERENCES {SHOT}.shot_revisions (entity_id, project_id, tenant_id, organization_id)"
        f" DEFERRABLE INITIALLY DEFERRED" + ")"
    )


# ---------------------------------------------------------------------------
# RLS：新表必须与既有归属表同形，否则隔离项目从这两张表继续可见
# ---------------------------------------------------------------------------
#: (表全名, schema, 表名, 行键列)。行键列用于 `repair_row_allowed` 与
#: `repair_row_scope`——`shots` 用 `shot_id`（= entity_id），`scenes` 用 `scene_id`。
RLS_TABLES: tuple[tuple[str, str, str, str], ...] = (
    (f"{PLANNING}.scenes", PLANNING, "scenes", "scene_id"),
    (f"{SHOT}.shots", SHOT, "shots", "shot_id"),
)


def _rls_statements(table: str, schema: str, name: str, key_col: str) -> list[str]:
    """生成 5 条策略。

    与 `episodes` / `shot_revisions` 逐条同形，理由不是复制粘贴省事，而是
    「隔离项目对业务侧不可见」这条不变量一旦在某张表上形状不同，它就在那张表上
    不成立——而 RLS 的失败方式是**静默返回 0 行**，没有任何报错会提醒。
    """
    org_and_project = (
        "(organization_id = (NULLIF(current_setting('app.current_org_id', true), ''))::uuid "
        "AND (current_setting('app.current_projects', true) = '*' "
        "OR project_id::text = ANY (string_to_array("
        "NULLIF(current_setting('app.current_projects', true), ''), ','))))"
    )
    quarantine_using = (
        "(project_id IS NULL) OR (NOT project_planning.is_quarantine_project(project_id::text)) "
        "OR pg_has_role(CURRENT_USER, 'afs_data_repair', 'member') "
        "OR (current_setting('app.include_quarantine', true) = 'on')"
    )
    # CHECK 侧多一个 `pg_has_role(afs_audit)`：审计可以「看见」隔离行，
    # 但不能顺手把业务行「写进」隔离区。USING 与 CHECK 不对称是刻意的。
    quarantine_check = (
        "(project_id IS NULL) OR (NOT project_planning.is_quarantine_project(project_id::text)) "
        "OR pg_has_role(CURRENT_USER, 'afs_data_repair', 'member') "
        "OR ((current_setting('app.include_quarantine', true) = 'on') "
        "AND pg_has_role(CURRENT_USER, 'afs_audit', 'member'))"
    )
    repair_pred = (
        "current_setting('app.ownership_repair_ticket', true) IS NOT NULL AND EXISTS ("
        " SELECT 1 FROM project_planning.ownership_repair_map m"
        f" WHERE m.table_name = '{table}'"
        f"   AND m.row_key = {key_col}::text"
        "   AND m.ticket_ref = current_setting('app.ownership_repair_ticket', true)"
        "   AND m.applied_at IS NULL)"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        # 作用域：RESTRICTIVE——组织与项目集必须同时匹配。写成 PERMISSIVE 时
        # 任何一条别的宽松策略都会把它或掉，作用域就形同虚设。
        f"CREATE POLICY project_scope_{name} ON {table} AS RESTRICTIVE FOR ALL TO PUBLIC "
        f"USING ({org_and_project} OR project_planning.repair_row_allowed('{table}', {key_col}::text)) "
        f"WITH CHECK ({org_and_project} OR project_planning.repair_row_allowed('{table}', {key_col}::text))",
        f"CREATE POLICY tenant_isolation_{name} ON {table} AS PERMISSIVE FOR ALL TO PUBLIC "
        f"USING (tenant_id = (NULLIF(current_setting('app.current_tenant_id', true), ''))::uuid)",
        f"CREATE POLICY quarantine_restrictive_{name} ON {table} AS RESTRICTIVE FOR ALL TO PUBLIC "
        f"USING ({quarantine_using}) WITH CHECK ({quarantine_check})",
        f"CREATE POLICY audit_quarantine_scope ON {table} AS PERMISSIVE FOR ALL TO afs_audit "
        "USING (current_setting('app.include_quarantine', true) = 'on') "
        "WITH CHECK (current_setting('app.include_quarantine', true) = 'on')",
        f"CREATE POLICY repair_row_scope ON {table} AS PERMISSIVE FOR ALL TO afs_data_repair "
        f"USING ({repair_pred}) WITH CHECK ({repair_pred})",
    ]


#: 授权。角色按域归属，不按「谁可能需要」——
#:   scenes 归 project_planning 域（与 episodes 同层），shots 归 shot_planning 域；
#:   afs_audit 只读（审计不是操作方）；
#:   afs_data_repair 需 SELECT + UPDATE（归属订正），不给 INSERT/DELETE。
GRANTS: tuple[tuple[str, str, str], ...] = (
    (f"{PLANNING}.scenes", "svc_project_planning", "SELECT, INSERT, UPDATE, DELETE"),
    (f"{PLANNING}.scenes", "afs_audit", "SELECT"),
    (f"{PLANNING}.scenes", "afs_data_repair", "SELECT, UPDATE"),
    (f"{SHOT}.shots", "svc_shot_planning", "SELECT, INSERT, UPDATE, DELETE"),
    (f"{SHOT}.shots", "afs_audit", "SELECT"),
    (f"{SHOT}.shots", "afs_data_repair", "SELECT, UPDATE"),
)


#: 把各域已有的 `shot_id` / `scene_id` 接到新表上。
#:
#: 这一批就是闸门里的 ``DANGLING``。**只建表不接边等于白建**：台账会从
#: 「父表不存在」变成「父表存在但无外键」，红项一条不少，而看起来做了很多事。
#:
#: 一律用 `(project_id, 引用列)` 复合形状：
#: 单列只能证明父行存在，证明不了「挂它的这行在本项目」。`shots` / `scenes`
#: 都已备 `UNIQUE (project_id, <identity>)` 作为目标键。
CONVERGENCE_FK: tuple[tuple[str, str, str, str, str], ...] = (
    (
        f"{SHOT}.shot_specs",
        "fk_shot_specs_shot_project",
        "project_id, shot_id",
        f"{SHOT}.shots",
        "project_id, shot_id",
    ),
    (
        f"{SHOT}.shot_visual_plans",
        "fk_shot_visual_plans_shot_project",
        "project_id, shot_id",
        f"{SHOT}.shots",
        "project_id, shot_id",
    ),
    (
        f"{GEN}.generation_tasks",
        "fk_generation_tasks_shot_project",
        "project_id, shot_id",
        f"{SHOT}.shots",
        "project_id, shot_id",
    ),
    (
        f"{GEN}.shot_route_selections",
        "fk_shot_route_selections_shot_project",
        "project_id, shot_id",
        f"{SHOT}.shots",
        "project_id, shot_id",
    ),
    (
        f"{SHOT}.constraint_packs",
        "fk_constraint_packs_scene_project",
        "project_id, scene_id",
        f"{PLANNING}.scenes",
        "project_id, scene_id",
    ),
)


#: 三列归属外键（与 `projects` 对齐）。
#:
#: 这两条写在 `CREATE TABLE` 里，因此**天然 `convalidated = true`**——
#: DDL 不允许新建表时声明 `NOT VALID`（没有历史行可豁免）。台账要如实反映这一点：
#: 别的边可以是「已建待验证」，这两条只能直接就是「已验证」，把它们算成待验证
#: 是伪造状态。
#: 本批次建立的全部归属/一致外键。
#:
#: 名字沿用 `OWNERSHIP_FKS`，因为消费方只做一件事：逐条 `VALIDATE`。
#: 漏列一条，`--validate` 就不会去验它——台账上表现为「永远待验证」，
#: 而待验证与真违规在报告里只差一个词，最容易被当成「稍后再跑」。
OWNERSHIP_FKS: tuple[tuple[str, str], ...] = (
    # 三列归属（随建表生效，DDL 无法声明 NOT VALID）
    (f"{PLANNING}.scenes", "fk_scenes_project_tenant_org"),
    (f"{SHOT}.shots", "fk_shots_project_tenant_org"),
    # 新建表内的一致边（同样随建表生效）
    (f"{SHOT}.shots", "fk_shots_scene_project"),
    (f"{SHOT}.shots", "fk_shots_current_revision"),
    (f"{PLANNING}.scenes", "fk_scenes_episode_project"),
    # 后加的一致边（NOT VALID，需显式 VALIDATE）
    (f"{SHOT}.shot_revisions", "fk_shot_revisions_episode_identity"),
    (f"{SHOT}.shot_revisions", "fk_shot_revisions_scene_project"),
    (f"{SHOT}.shot_revisions", "fk_shot_revisions_shot_project"),
    (f"{SHOT}.shot_specs", "fk_shot_specs_shot_project"),
    (f"{SHOT}.shot_visual_plans", "fk_shot_visual_plans_shot_project"),
    (f"{GEN}.generation_tasks", "fk_generation_tasks_shot_project"),
    (f"{GEN}.shot_route_selections", "fk_shot_route_selections_shot_project"),
    (f"{SHOT}.constraint_packs", "fk_constraint_packs_scene_project"),
)


def applied_probe_sql() -> str:
    """本层是否已应用：以 `scenes` 表存在为探针。

    选两张表里先建的那张，且它**最先创建、最后撤销**，故「存在 ⇔ 本层已应用」。
    """
    return f"SELECT count(*) FROM pg_class WHERE oid = to_regclass('{PLANNING}.scenes')"


def upgrade_steps() -> list[Step]:
    steps: list[Step] = [
        # 1) 外键的可引用目标键。`episodes` 只有 PK(entity_id) 与
        #    UNIQUE(project_id, episode_ref)，没有 (entity_id, project_id)，
        #    缺它 `scenes.episode_id` 的复合外键会直接 42830。
        Step(
            f"ALTER TABLE {PLANNING}.episodes "
            "ADD CONSTRAINT uq_episodes_entity_project UNIQUE (entity_id, project_id)"
        ),
        # 2) 场次
        Step(_create_scenes_sql()),
    ]
    for table, schema, name, key_col in RLS_TABLES:
        if name != "scenes":
            continue
        steps.extend(Step(s) for s in _rls_statements(table, schema, name, key_col))

    # 3) `shot_revisions` 的正式/草案分层收敛。
    #    先加可空列：直接 NOT NULL 会让「有数据但无法确定归属」的库在 ALTER 处
    #    失败，而错误信息只是一个模糊的 NOT NULL 违规，看不出是哪一行、为什么。
    steps += [
        Step(f"ALTER TABLE {SHOT}.shot_revisions ADD COLUMN episode_id varchar"),
        Step(f"ALTER TABLE {SHOT}.shot_revisions ADD COLUMN scene_id varchar"),
        # 回填走已有的 `episode_ref`（它本身有 (project_id, episode_ref) → episodes 外键，
        # 故凡是能回填的行，其 episode_ref 一定真实存在）。
        Step(
            f"UPDATE {SHOT}.shot_revisions r SET episode_id = e.entity_id "
            f"FROM {PLANNING}.episodes e "
            "WHERE e.project_id = r.project_id AND e.episode_ref = r.episode_ref "
            "AND r.episode_id IS NULL"
        ),
        # scene_id 没有可回填的来源：`scenes` 表刚建、必然为空，而
        # `shot_revisions.scene_ref` 是草案期业务编码，与正式场次身份无对应关系。
        # 故这里刻意**不猜**，改为断言：有存量而不为空说明有人绕过确认入口写入了
        # 正式修订（该入口尚未实现），必须由人判断归属，不能让迁移静默置空。
        Step(
            "DO $afs$ DECLARE n_ep bigint; n_sc bigint; "
            f"BEGIN "
            f"SELECT count(*) INTO n_ep FROM {SHOT}.shot_revisions WHERE episode_id IS NULL; "
            f"SELECT count(*) INTO n_sc FROM {SHOT}.shot_revisions WHERE scene_id IS NULL; "
            "IF n_ep > 0 THEN RAISE EXCEPTION "
            "'shot_revisions 有 % 行的 episode_ref 在 episodes 中不存在，无法确定正式归属。"
            "正式修订必须能指向真实集（契约 ShotRevision.episode_id 必填）；"
            "请先裁定这些行的归属，不要指望迁移替它猜。', n_ep; END IF; "
            "IF n_sc > 0 THEN RAISE EXCEPTION "
            "'shot_revisions 有 % 行无法确定 scene_id。场次身份不可由 scene_ref 反推——"
            "那是草案期业务编码。请确认这些修订对应的正式场次后重跑。', n_sc; END IF; "
            "END $afs$"
        ),
        Step(f"ALTER TABLE {SHOT}.shot_revisions ALTER COLUMN episode_id SET NOT NULL"),
        Step(f"ALTER TABLE {SHOT}.shot_revisions ALTER COLUMN scene_id SET NOT NULL"),
        # 4) 正式修订的归属边。NOT VALID：不扫历史行（本库为空，但迁移不假设目标库干净），
        #    新写入立即受约束；VALIDATE 由台账流程逐条执行。
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions ADD CONSTRAINT fk_shot_revisions_episode_identity "
            f"FOREIGN KEY (episode_id, project_id) REFERENCES {PLANNING}.episodes (entity_id, project_id) "
            "NOT VALID"
        ),
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions ADD CONSTRAINT fk_shot_revisions_scene_project "
            f"FOREIGN KEY (project_id, scene_id) REFERENCES {PLANNING}.scenes (project_id, scene_id) "
            "NOT VALID"
        ),
        # 5) 镜头
        Step(_create_shots_sql()),
    ]
    for table, schema, name, key_col in RLS_TABLES:
        if name != "shots":
            continue
        steps.extend(Step(s) for s in _rls_statements(table, schema, name, key_col))

    # 6) 接边。必须全部晚于 `shots` 建表。
    #
    #    `shot_revisions` 这条可延迟：它与 `shots.shot_revision_id` 互指
    #    （ADR-R18 要求同一事务创建），事务内先插哪一方都不该失败。
    steps.append(
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions ADD CONSTRAINT fk_shot_revisions_shot_project "
            f"FOREIGN KEY (project_id, shot_id) REFERENCES {SHOT}.shots (project_id, shot_id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID"
        )
    )
    steps.extend(
        Step(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({cols}) "
            f"REFERENCES {parent} ({pcols}) NOT VALID"
        )
        for table, name, cols, parent, pcols in CONVERGENCE_FK
    )

    steps.extend(Step(f"GRANT {priv} ON {table} TO {role}") for table, role, priv in GRANTS)
    return steps


def downgrade_steps() -> list[Step]:
    """严格对称：撤销本层后库应等价于 `h8c9` 时刻。

    顺序与升级相反。两处必须显式处理：
    - `shots` 先于 `shot_revisions` 的新列撤销：`shots.shot_revision_id` 依赖
      `shot_revisions` 的四列唯一键，先撤列会让 `shots` 的外键悬空（DROP COLUMN
      会级联删约束，但那是隐式行为，不该依赖）。
    - `episodes` 的唯一键最后撤销：它是 `scenes` 外键的目标键。
    """
    steps: list[Step] = []
    for table, role, priv in reversed(GRANTS):
        steps.append(Step(f"REVOKE {priv} ON {table} FROM {role}"))

    steps.append(
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions DROP CONSTRAINT IF EXISTS fk_shot_revisions_shot_project"
        )
    )
    for table, name, _cols, _parent, _pcols in reversed(CONVERGENCE_FK):
        steps.append(Step(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
    steps.append(Step(f"DROP TABLE IF EXISTS {SHOT}.shots"))
    steps.append(
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions DROP CONSTRAINT IF EXISTS fk_shot_revisions_scene_project"
        )
    )
    steps.append(
        Step(
            f"ALTER TABLE {SHOT}.shot_revisions DROP CONSTRAINT IF EXISTS fk_shot_revisions_episode_identity"
        )
    )
    steps.append(Step(f"ALTER TABLE {SHOT}.shot_revisions DROP COLUMN IF EXISTS scene_id"))
    steps.append(Step(f"ALTER TABLE {SHOT}.shot_revisions DROP COLUMN IF EXISTS episode_id"))
    steps.append(Step(f"DROP TABLE IF EXISTS {PLANNING}.scenes"))
    steps.append(
        Step(
            f"ALTER TABLE {PLANNING}.episodes DROP CONSTRAINT IF EXISTS uq_episodes_entity_project"
        )
    )
    return steps


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
