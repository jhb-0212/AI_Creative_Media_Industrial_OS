"""归属「一致」外键：把「本行 project 与父行相同」从约定变成数据库事实。

为什么必须有这一批
------------------
前三批做的是 I-1 的**「真实」半边**：`(project_id, tenant_id, organization_id)`
指向 `projects`，保证目标项目存在且组织一致。它拦不住下面这件事：

    qc_reports(qc-1) 的 asset_version_id 指向 av-1（属于 proj-alpha），
    而 qc-1 自己的 project_id 写成 proj-beta。两个项目都真实、组织也一致，
    三列外键一路放行。

于是同一条 QC 事实在「按资产溯源」与「按项目统计」两个口径下归属不同。
本库实测就存在这样一行（`qc_reports` 唯一一行），不是假想风险。

`verify_ownership_consistency.py` 把这条规则做成了闸门，并在修复前跑出 19 条红：
其中 1 条是 ``FK_NOT_COMPOSITE``——`qc_reports.asset_version_id` **已有**单列外键，
看起来「已经保护了」，实际只保父行存在。这类「看起来做了」比「没做」更危险，
所以本迁移要**替换**它，而不是在旁边再加一条。

判定为什么只需要 `(父引用列, project_id)`
----------------------------------------
复合外键把本行 `project_id` 钉到父行的 `project_id` 上 ⇒ 两者指向 `projects` 的
**同一行**；两边各自的 `(project_id, tenant_id, organization_id) → projects` 三列外键
又把这个 project 钉到唯一一组 `(tenant, org)` 上 ⇒ tenant/org 一致性**传递成立**。
故不必在复合外键里重复 tenant/org，只在父表缺少等价唯一键时补一个。

`script_revisions` 早就用 `(project_id, episode_ref) → episodes` 做到了这件事，
本迁移只是把它推广到其余边——不是发明新机制。

顺序（不可倒）
--------------
1. 父表补唯一键（外键要有可引用的目标键；缺键时 `ADD FOREIGN KEY` 直接 42830，
   与数据脏无关——空表上一样报）。
2. 删除被取代的单列外键（消除重复机制：两条键约束同一列时，保留弱的那条只会
   让人误以为保护存在）。
3. 挂复合外键 `NOT VALID`（不扫历史行，新写入立即受约束）。

为什么不在此处 `VALIDATE`、也不在此处订正存量
--------------------------------------------
数据订正要走**受控例外**（`afs_data_repair` + 工单令牌 + 映射行 + 仅归属三列），
而例外是给运维操作留的通道，不是给迁移用的。把订正塞进迁移有两个后果：
一是迁移会在不同库上依赖不同的脏数据分布（不可复现），二是守卫一旦拦下来，
整批 DDL 回滚，看起来像「结构变更失败」。

所以三段分开，各司其职：

    本迁移（结构） → repair_ownership.py（走例外的存量订正）
                   → acceptance_ledger.py --validate（闭环判定）

本迁移**不假设目标库干净**：`NOT VALID` 就是为「存量可能脏」准备的姿势。
虚报「闭环」的责任不在这里，在台账。

本迁移**不解决**的
------------------
`shot_id` / `scene_id` / `gate_instance_id` 这几列指向的 `shots` / `scenes` /
`gate_instances` 表在库中**不存在**，属镜头域运行时未落地。它们无法用外键修，
也不能归入「不是边」——归入即等于把「未实施」永久洗白。闸门对它们报
``DANGLING`` 并单列，这是当前状态的准确描述，不是本批次能消除的缺口。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "h8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "g7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
SHOT = "shot_planning"
GEN = "generation_execution"
ASSET = "asset_service"
QC = "qc_governance"
WF = "workflow"


class Step(NamedTuple):
    """一步一条语句。

    刻意限制为单语句：多语句串在一次 ``execute`` 里时，任一句失败整批回滚，
    无法判断到底哪一句失败（归属栈第一批期间踩过）。

    用 ``NamedTuple`` 而非 ``@dataclass``：Alembic 用
    ``importlib.util.module_from_spec`` 加载迁移却不先登记 ``sys.modules``，
    dataclass 解析注解时会反查 ``sys.modules[cls.__module__]`` 拿到 ``None``。
    """

    sql: str


# ---------------------------------------------------------------------------
# 1) 父表补唯一键：外键的可引用目标
# ---------------------------------------------------------------------------
#: 四列唯一键。列集合必须与子表复合外键的引用列一一对应；
#: 顺序无关，但为可读性统一写成 (身份, project, tenant, org)。
PARENT_UNIQUE_4: tuple[tuple[str, str, str], ...] = (
    # (schema.table, 约束名, 身份列)
    (f"{ASSET}.asset_versions", "uq_asset_versions_entity_project_tenant_org", "entity_id"),
    (f"{SHOT}.shot_revisions", "uq_shot_revisions_entity_project_tenant_org", "entity_id"),
    (f"{ASSET}.qc_reports", "uq_qc_reports_id_project_tenant_org", "qc_report_id"),
    (f"{WF}.approval_flows", "uq_approval_flows_entity_project_tenant_org", "entity_id"),
    (f"{WF}.approval_tickets", "uq_approval_tickets_entity_project_tenant_org", "entity_id"),
)

#: 组织级父表的两列唯一键。`production_routes` 是全组织复用的路线库，
#: 自身没有 `project_id` —— 指向它的边只能锁到组织层，这不是放宽要求，
#: 而是父行本身就到组织为止。
PARENT_UNIQUE_2: tuple[tuple[str, str, str], ...] = (
    (f"{GEN}.production_routes", "uq_production_routes_entity_org", "entity_id"),
)


# ---------------------------------------------------------------------------
# 2) 被取代的单列外键：必须删，否则「弱的那条」会被误读成保护仍在
# ---------------------------------------------------------------------------
SUPERSEDED_FK: tuple[tuple[str, str], ...] = (
    (f"{ASSET}.qc_reports", "qc_reports_asset_version_id_fkey"),
    (f"{GEN}.shot_route_selections", "fk_generation_execution_shot_route_selections_route_id"),
)


# ---------------------------------------------------------------------------
# 3) 复合外键（NOT VALID）
# ---------------------------------------------------------------------------
#: (schema.table, 约束名, 子表列序列, 父表, 父表列序列, 尾缀如 ON DELETE)
COMPOSITE_FK: tuple[tuple[str, str, tuple[str, ...], str, tuple[str, ...], str], ...] = (
    # 资产域
    (
        f"{ASSET}.qc_reports",
        "fk_qc_reports_asset_version_project",
        ("asset_version_id", "project_id", "tenant_id", "organization_id"),
        f"{ASSET}.asset_versions",
        ("entity_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    (
        f"{QC}.qc_review_sessions",
        "fk_qc_review_sessions_asset_version_project",
        ("asset_version_id", "project_id", "tenant_id", "organization_id"),
        f"{ASSET}.asset_versions",
        ("entity_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    (
        f"{QC}.qc_review_sessions",
        "fk_qc_review_sessions_qc_report_project",
        ("qc_report_ref", "project_id", "tenant_id", "organization_id"),
        f"{ASSET}.qc_reports",
        ("qc_report_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    # 镜头域：规格随修订
    (
        f"{SHOT}.shot_specs",
        "fk_shot_specs_shot_revision_project",
        ("shot_revision_id", "project_id", "tenant_id", "organization_id"),
        f"{SHOT}.shot_revisions",
        ("entity_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    # 镜头域：草案/修订随分集。业务键 (project_id, episode_ref) 本身已含 project，
    # 故两列即可锁死一致性，无需四列——`script_revisions` 早就是这个形状。
    (
        f"{SHOT}.shot_drafts",
        "fk_shot_drafts_episode_project",
        ("project_id", "episode_ref"),
        f"{PLANNING}.episodes",
        ("project_id", "episode_ref"),
        "",
    ),
    (
        f"{SHOT}.shot_revisions",
        "fk_shot_revisions_episode_project",
        ("project_id", "episode_ref"),
        f"{PLANNING}.episodes",
        ("project_id", "episode_ref"),
        "",
    ),
    # 审批域：票据随流程、步骤随票据
    (
        f"{WF}.approval_tickets",
        "fk_approval_tickets_flow_project",
        ("flow_id", "project_id", "tenant_id", "organization_id"),
        f"{WF}.approval_flows",
        ("entity_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    (
        f"{WF}.approval_steps",
        "fk_approval_steps_ticket_project",
        ("ticket_id", "project_id", "tenant_id", "organization_id"),
        f"{WF}.approval_tickets",
        ("entity_id", "project_id", "tenant_id", "organization_id"),
        "",
    ),
    # 组织级边：路线库跨项目复用，只能要求同组织
    (
        f"{GEN}.generation_tasks",
        "fk_generation_tasks_route_org",
        ("route_id", "organization_id"),
        f"{GEN}.production_routes",
        ("entity_id", "organization_id"),
        "",
    ),
    (
        f"{GEN}.shot_route_selections",
        "fk_shot_route_selections_route_org",
        ("route_id", "organization_id"),
        f"{GEN}.production_routes",
        ("entity_id", "organization_id"),
        " ON DELETE RESTRICT",
    ),
)


def _cols(cols: tuple[str, ...]) -> str:
    return ", ".join(cols)


#: 供验收装置使用的外键清单 (table, constraint)。与前三批的 `OWNERSHIP_FKS` 同形，
#: 让 `acceptance_ledger` 的自检能**统一**遍历各批，而不必为每批写一个特例分支。
#: 只暴露「子表 + 约束名」——装置需要的正是这两项；把父表、列序也塞进来会让
#: 装置有机会自己重算一遍语义，那等于第二份定义。
OWNERSHIP_FKS: tuple[tuple[str, str], ...] = tuple(
    (table, name) for table, name, *_ in COMPOSITE_FK
)


def applied_probe_sql() -> str:
    """本层是否已应用：以「父表四列唯一键」为探针。

    选唯一键而不是外键，是因为唯一键在 `upgrade_steps()` 里**最先**创建、
    在 `downgrade_steps()` 里**最后**撤销：它存在 ⇔ 本层处于「已应用」状态。
    用外键当探针会在「外键已删、唯一键还在」的半回退状态上判错，
    而防止半回退正是这个探针要服务的场景。

    `lab.rewind_to` 靠它区分「该层已应用、需要回退」与「该层从未应用、跳过」。
    """
    return (
        "SELECT count(*) FROM pg_constraint WHERE contype = 'u' "
        "AND conname = 'uq_asset_versions_entity_project_tenant_org'"
    )


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    for table, name, key in PARENT_UNIQUE_4:
        steps.append(
            Step(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"UNIQUE ({key}, project_id, tenant_id, organization_id)"
            )
        )
    for table, name, key in PARENT_UNIQUE_2:
        steps.append(
            Step(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({key}, organization_id)")
        )

    # 先删弱键、再加复合键。反过来会出现一个短暂窗口：两条键同时约束同一列，
    # 若此时有人读系统目录，会看到「保护已经存在」的假象。
    for table, name in SUPERSEDED_FK:
        steps.append(Step(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))

    for table, name, child_cols, parent, parent_cols, tail in COMPOSITE_FK:
        steps.append(
            Step(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"FOREIGN KEY ({_cols(child_cols)}) "
                f"REFERENCES {parent} ({_cols(parent_cols)}){tail} NOT VALID"
            )
        )
    return steps


def downgrade_steps() -> list[Step]:
    """逆序撤销：外键 → 唯一键 → 恢复被取代的单列外键。

    恢复单列外键不是为了「回到更差的状态」，而是让 downgrade 真正对称：
    撤销本批次后，库应当与 `g7b8c9d0e1f2` 时刻**等价**。少恢复一条，
    「回退到基线」就会静默丢掉一条曾经存在过的保护，而没有任何断言会报错。

    恢复前刻意先 `DROP ... IF EXISTS`：`ADD CONSTRAINT` 没有幂等写法，若不先删，
    在「本层其实未应用」的库上执行降级会以 `42710` 失败——而那种库上降级**本就
    应该什么都不做**。`lab.rewind_to` 已用 `applied_probe_sql()` 避免进入这条路径，
    这里再加一层幂等，是为了让「直接用 downgrade 脚本回退」的场合也不炸。
    """
    steps: list[Step] = []
    for table, name, *_ in reversed(COMPOSITE_FK):
        steps.append(Step(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
    for table, name, _key in reversed(PARENT_UNIQUE_2):
        steps.append(Step(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
    for table, name, _key in reversed(PARENT_UNIQUE_4):
        steps.append(Step(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))

    steps.append(
        Step(
            f"ALTER TABLE {ASSET}.qc_reports "
            f"DROP CONSTRAINT IF EXISTS qc_reports_asset_version_id_fkey"
        )
    )
    steps.append(
        Step(
            f"ALTER TABLE {ASSET}.qc_reports ADD CONSTRAINT qc_reports_asset_version_id_fkey "
            f"FOREIGN KEY (asset_version_id) REFERENCES {ASSET}.asset_versions (entity_id)"
        )
    )
    steps.append(
        Step(
            f"ALTER TABLE {GEN}.shot_route_selections "
            f"DROP CONSTRAINT IF EXISTS fk_generation_execution_shot_route_selections_route_id"
        )
    )
    steps.append(
        Step(
            f"ALTER TABLE {GEN}.shot_route_selections "
            f"ADD CONSTRAINT fk_generation_execution_shot_route_selections_route_id "
            f"FOREIGN KEY (route_id) REFERENCES {GEN}.production_routes (entity_id) "
            f"ON DELETE RESTRICT"
        )
    )
    return steps


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
