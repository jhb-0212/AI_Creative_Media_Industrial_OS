"""18 行归属闭环验收台账（ADR-R40 §13 D-10.1 / 附录 J.2 的唯一验收判据）。

判据（三条件合取，缺一即不通过）::

    PASS(t) ⇔ 约束存在(t) ∧ convalidated(t) = true ∧ 违规行数(t) = 0

为什么必须写成脚本而不是一张人工填的表：
D-10.1 明确拒绝「用文档声明替代真实数据库验证」「将未验证约束标记为通过」。
一张手填的台账无法区分「真的验过了」和「以为验过了」，也无法在数据变脏后自己变红。
本脚本的每条 PASS 都来自 `pg_constraint` 实读 + 逐表 `LEFT JOIN` 实算，
且**在脏库上必须输出 FAIL**——`--selftest` 会证明它不是恒真。

用法::

    python docs/audits/mig-recon/acceptance_ledger.py --db ai_film_industrial --port 15444
    python docs/audits/mig-recon/acceptance_ledger.py --selftest   # 三种库各跑一次，证明可 FAIL
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

import lab

REPO = Path(__file__).resolve().parents[3]
OUT_MD = Path(__file__).resolve().parent / "acceptance-ledger.md"
OUT_JSON = Path(__file__).resolve().parent / "acceptance-ledger.json"

PROJECTS = "project_planning.projects"
ORGS = "project_planning.organizations"


@dataclass(frozen=True)
class Row:
    """台账一行：一张表 + 它的目标约束 + 归属类型。

    `kind` 的三档对应两类不变量（D-10.2）：

    * ``A``  —— 项目级「真实」：`(project_id, tenant_id, organization_id) → projects`
    * ``B``  —— 组织级「真实」：`(organization_id, tenant_id) → organizations`
    * ``ROOT`` —— `projects` 自身挂到 `organizations`（链终点）
    * ``A2`` —— 项目级「一致」：本行 `project_id` 必须等于**父行**的 `project_id`

    A2 是本轮新增的一档。之前台账只有前三档，于是「约束全 PASS」可以被读成
    「保护完备」——而 I-1 的「一致」半边根本没被任何一行覆盖。这与 `qc_reports`
    落在闭环外是同一类错误：**没被列入的判断等于不存在**。

    `A2` 的父子关系用 ``parent_cols`` 显式写出（父列, 子列），不按命名推导：
    父键有时是身份列（`asset_versions.entity_id`）、有时是业务键
    （`episodes.(project_id, episode_ref)`），推导会猜错，而猜错的后果是
    「JOIN 上了但比错了列」，比不检查更糟。
    """

    table: str
    kind: str  # "A" 项目级 / "B" 组织级 / "ROOT" 归属根 / "A2" 一致半边
    constraint: str
    parent: str | None = None
    parent_cols: tuple[tuple[str, str], ...] = ()  # (父列, 子列)
    parent_pk: str | None = None  # 判「父行是否存在」用的父表主键列
    org_level: bool = False  # 父表无 project_id（组织级父行）
    note: str = ""


def ledger_rows() -> list[Row]:
    """18 行。前 17 行是域表（含 1 张组织级），第 18 行是归属根 projects。

    **为什么从 17 涨到 18**：`episodes` 是第三张「落在闭环之外」的表，而且比前两张更
    值得记一笔——它**的例外通道早就完整建好了**：`c1d2` 给了 `project_scope_episodes`，
    `d2e3` 给了修数权限，`e3f4` 给了 RLS 旁路。整套修数机制是照着「它的 project_id
    可能会指错、需要订正」这个前提搭的，唯独「指错」本身没有任何约束去定义。
    它不是被判断错，而是从未进入判断（既不在 `PROJECT_LEVEL`，也不在 `DERIVED_NO_FK`）。

    这张表由**覆盖闸门**抓出来，不是靠人复查：
    `verify_ownership_closure.py` 从 `pg_attribute` 实读带归属列的表，与台账集合求差，
    差集非空即 FAIL。闸门先行、本行后补——顺序不能反，反过来闸门就成了给现状背书。

    **为什么从 14 涨到 17**：09 版把 `qc_reports` / `shot_specs` / `approval_steps`
    排除在外——`qc_reports` 是漏了（既不在 PROJECT_LEVEL 也不在 DERIVED_NO_FK），
    另两张是「刻意不建」。逐表覆盖矩阵随后证明这个排除有代价：三张表都在不可变
    守卫下、都被受控例外覆盖，例外放行后能把 `project_id` 改成不存在的项目且
    数据库不拦（实测 UPDATE 1 成功）。归属事实已落库，却在闭环之外。
    故 `f8a9b0c1d2e3` 补了三条三列外键，台账同步纳入——不是为了让数字好看，
    而是原先的数字掩盖了三张真实的表。

    仍然**不含**的是：任何没有 `project_id`/`organization_id` 事实列的表。
    台账的行集必须能逐行对应一条真实约束，凑数没有意义。

    **为什么再加 10 行 A2**：前 18 行全部是「真实」半边。它全绿时，
    `qc_reports` 唯一一行仍然可以「挂在 proj-alpha 的资产上、自己写 proj-beta」——
    两个项目都真实，三列外键一路放行。这不是理论缺口，本库实测就是这样。
    所以台账再加一节，逐条覆盖「本行 project 必须等于父行 project」。
    本节由 `verify_ownership_consistency.py` 的边分类派生校验，两处不可分叉。
    """
    return [
        Row("project_planning.episodes", "A", "fk_episodes_project_tenant_org"),
        Row("generation_execution.production_routes", "B",
            "fk_production_routes_organization_tenant"),
        Row("generation_execution.generation_tasks", "A",
            "fk_generation_tasks_project_tenant_org"),
        Row("generation_execution.shot_route_selections", "A",
            "fk_shot_route_selections_project_tenant_org"),
        Row("shot_planning.shot_drafts", "A", "fk_shot_drafts_project_tenant_org"),
        Row("shot_planning.shot_revisions", "A", "fk_shot_revisions_project_tenant_org"),
        Row("shot_planning.shot_visual_plans", "A", "fk_shot_visual_plans_project_tenant_org"),
        Row("shot_planning.constraint_packs", "A", "fk_constraint_packs_project_tenant_org"),
        Row("shot_planning.shot_specs", "A", "fk_shot_specs_project_tenant_org"),
        Row("asset_service.delivery_manifests", "A",
            "fk_delivery_manifests_project_tenant_org"),
        Row("asset_service.asset_versions", "A", "fk_asset_versions_project_tenant_org"),
        Row("asset_service.qc_reports", "A", "fk_qc_reports_project_tenant_org"),
        Row("qc_governance.gate_decisions", "A", "fk_gate_decisions_project_tenant_org"),
        Row("qc_governance.qc_review_sessions", "A",
            "fk_qc_review_sessions_project_tenant_org"),
        Row("workflow.approval_flows", "A", "fk_approval_flows_project_tenant_org"),
        Row("workflow.approval_tickets", "A", "fk_approval_tickets_project_tenant_org"),
        Row("workflow.approval_steps", "A", "fk_approval_steps_project_tenant_org"),
        Row(PROJECTS, "ROOT", "fk_projects_organization_tenant"),

        # ---- 一致半边（A2）：本行 project 必须等于父行 project ----
        Row("asset_service.qc_reports", "A2", "fk_qc_reports_asset_version_project",
            parent="asset_service.asset_versions",
            parent_cols=(("entity_id", "asset_version_id"),),
            parent_pk="entity_id",
            note="QC 报告随被检资产版本"),
        Row("qc_governance.qc_review_sessions", "A2",
            "fk_qc_review_sessions_asset_version_project",
            parent="asset_service.asset_versions",
            parent_cols=(("entity_id", "asset_version_id"),),
            parent_pk="entity_id",
            note="QC 会话随被检资产版本"),
        Row("qc_governance.qc_review_sessions", "A2",
            "fk_qc_review_sessions_qc_report_project",
            parent="asset_service.qc_reports",
            parent_cols=(("qc_report_id", "qc_report_ref"),),
            parent_pk="qc_report_id",
            note="QC 会话随其引用的报告"),
        Row("shot_planning.shot_specs", "A2", "fk_shot_specs_shot_revision_project",
            parent="shot_planning.shot_revisions",
            parent_cols=(("entity_id", "shot_revision_id"),),
            parent_pk="entity_id",
            note="规格随镜头修订"),
        Row("shot_planning.shot_drafts", "A2", "fk_shot_drafts_episode_project",
            parent="project_planning.episodes",
            parent_cols=(("project_id", "project_id"), ("episode_ref", "episode_ref")),
            parent_pk="entity_id",
            note="草案按集组织（业务键已含 project，一致由结构保证）"),
        Row("shot_planning.shot_revisions", "A2", "fk_shot_revisions_episode_project",
            parent="project_planning.episodes",
            parent_cols=(("project_id", "project_id"), ("episode_ref", "episode_ref")),
            parent_pk="entity_id",
            note="修订按集组织（业务键已含 project，一致由结构保证）"),
        Row("workflow.approval_tickets", "A2", "fk_approval_tickets_flow_project",
            parent="workflow.approval_flows",
            parent_cols=(("entity_id", "flow_id"),),
            parent_pk="entity_id",
            note="票据随流程"),
        Row("workflow.approval_steps", "A2", "fk_approval_steps_ticket_project",
            parent="workflow.approval_tickets",
            parent_cols=(("entity_id", "ticket_id"),),
            parent_pk="entity_id",
            note="步骤随票据"),
        Row("generation_execution.generation_tasks", "A2", "fk_generation_tasks_route_org",
            parent="generation_execution.production_routes",
            parent_cols=(("entity_id", "route_id"),),
            parent_pk="entity_id",
            org_level=True,
            note="路线库跨项目复用，父行只到组织层"),
        Row("generation_execution.shot_route_selections", "A2",
            "fk_shot_route_selections_route_org",
            parent="generation_execution.production_routes",
            parent_cols=(("entity_id", "route_id"),),
            parent_pk="entity_id",
            org_level=True,
            note="路线库跨项目复用，父行只到组织层"),
    ]


def violation_sql(row: Row) -> str:
    """按归属类型选比对对象：A 对 projects，B/ROOT 对 organizations，A2 对父行。

    用 LEFT JOIN + IS NULL 而不是 `NOT EXISTS`，是为了让查询计划与「索引存在与否」
    无关地给出确定结果；也便于把该 SQL 直接贴进工单作为复核依据。

    A2 的 SQL 同时判两件事：**父行存在** ∧ **归属与父行相同**。只判后者会让
    「父行被删了」的子行算作合规（JOIN 不上就没有可比的父值），而那只保住了
    一半——前缀 `p.<pk> IS NULL` 就是为堵这个洞。
    """
    if row.kind == "A2":
        if not row.parent or not row.parent_cols or not row.parent_pk:
            raise ValueError(f"{row.table}/{row.constraint}：A2 行必须声明父子键")
        join = " AND ".join(f"p.{pk} = t.{ck}" for pk, ck in row.parent_cols)
        if row.org_level:
            cmp_cols = ("organization_id", "tenant_id")
        else:
            cmp_cols = ("project_id", "organization_id", "tenant_id")
        left = ", ".join(f"t.{c}" for c in cmp_cols)
        right = ", ".join(f"p.{c}" for c in cmp_cols)
        return f"""
            SELECT count(*) FROM {row.table} t
            LEFT JOIN {row.parent} p ON {join}
            WHERE p.{row.parent_pk} IS NULL
               OR ({left}) IS DISTINCT FROM ({right})
        """
    if row.kind == "A":
        return f"""
            SELECT count(*) FROM {row.table} t
            LEFT JOIN {PROJECTS} p
                   ON p.entity_id = t.project_id
                  AND p.tenant_id = t.tenant_id
                  AND p.organization_id = t.organization_id
            WHERE p.entity_id IS NULL
        """
    if row.kind == "B":
        return f"""
            SELECT count(*) FROM {row.table} t
            LEFT JOIN {ORGS} o
                   ON o.organization_id = t.organization_id
                  AND o.tenant_id = t.tenant_id
            WHERE o.organization_id IS NULL
        """
    return f"""
        SELECT count(*) FROM {PROJECTS} p
        LEFT JOIN {ORGS} o
               ON o.organization_id = p.organization_id
              AND o.tenant_id = p.tenant_id
        WHERE o.organization_id IS NULL
    """


async def collect(conn: asyncpg.Connection) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for row in ledger_rows():
        # 表可能整张不存在（未跑迁移的库、或该谱系没这张表）
        exists = bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", row.table))
        # 引用目标也可能不存在（分支谱系缺 organizations / 缺父表）——此时违规行数
        # 无法计算，必须显式报「不可计算」而不是报 0：报 0 会让台账在缺目标键的库上
        # 看起来干净。A2 行的「目标」是父表，同样要单独判存在性。
        if row.kind == "A":
            target = PROJECTS
        elif row.kind == "A2":
            target = row.parent
        else:
            target = ORGS
        target_exists = bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", target))

        con = None
        if exists:
            con = await conn.fetchrow(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = $1 AND conrelid = $2::regclass",
                row.constraint,
                row.table,
            )

        violations: int | None = None
        if exists and target_exists:
            violations = int(await conn.fetchval(violation_sql(row)))

        if not exists:
            verdict = "未建立（表不存在）"
        elif not target_exists:
            verdict = f"未建立（引用目标 {target} 不存在，违规行数不可计算）"
        elif con is None:
            verdict = "未建立（无约束）"
        elif con["convalidated"] and violations == 0:
            verdict = "PASS"
        elif con["convalidated"] and violations:
            verdict = "异常（已验证但仍有违规，说明校验后被写入绕过）"
        elif violations == 0:
            # 关键状态：约束在、新写入干净、但历史未验证 → 不是 PASS
            verdict = "待验证 PENDING_VALIDATE（NOT VALID 未闭环）"
        else:
            verdict = "未通过（存量违规未订正）"

        out.append(
            {
                "table": row.table,
                "kind": row.kind,
                "constraint": row.constraint,
                "parent": row.parent,
                "note": row.note,
                "table_exists": exists,
                "constraint_exists": con is not None,
                "convalidated": None if con is None else bool(con["convalidated"]),
                "violations": violations,
                "verdict": verdict,
                "pass": verdict == "PASS",
            }
        )
    return out


async def quarantine_stats(conn: asyncpg.Connection) -> dict[str, object]:
    """隔离区占比（D-10.3 要求 2 / 附录 E #25）：只应下降，持续上升须复审。"""
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", PROJECTS):
        return {"available": False}
    total = int(await conn.fetchval(f"SELECT count(*) FROM {PROJECTS}"))
    quarantine = int(
        await conn.fetchval(
            f"SELECT count(*) FROM {PROJECTS} WHERE entity_id LIKE 'quarantine-%'"
        )
    )
    return {
        "available": True,
        "project_total": total,
        "quarantine_projects": quarantine,
        "ratio": round(quarantine / total, 6) if total else 0.0,
    }


def render_md(rows: list[dict[str, object]], q: dict[str, object], meta: dict[str, object]) -> str:
    passed = sum(1 for r in rows if r["pass"])
    overall = f"PASS（{len(rows)}/{len(rows)} 闭环）" if passed == len(rows) else f"未通过（{passed}/{len(rows)}）"

    # 分两节渲染：两类不变量混在一张表里时，读者会自然地把「真实 18 行全绿」
    # 读成「保护完备」，而 I-1 的一致半边就在旁边空着。分开列并把小计写出来，
    # 让人一眼看到「这一节存在且是什么状态」，而不是只看到一个总数。
    real = [r for r in rows if r["kind"] != "A2"]
    consist = [r for r in rows if r["kind"] == "A2"]
    real_pass = sum(1 for r in real if r["pass"])
    consist_pass = sum(1 for r in consist if r["pass"])

    lines = [
        "# 归属闭环验收台账（机器生成，勿手改）",
        "",
        f"- 目标库：`{meta['db']}` @ `{meta['host']}:{meta['port']}`",
        f"- 采集时间：{meta['ts']}",
        "- 判据：`PASS ⇔ 约束存在 ∧ convalidated ∧ 违规行数 = 0`（ADR-R40 §13 D-10.1 / 附录 J.2）",
        f"- **整体判定：{overall}**",
        f"  - 「真实」半边（A/B/ROOT）：{real_pass}/{len(real)}",
        f"  - 「一致」半边（A2）：{consist_pass}/{len(consist)}",
        "",
        "## 一、「真实」半边：归属指向的实体真实存在且组织一致",
        "",
        "| # | 表 | 类型 | 目标约束 | 约束存在 | convalidated | 违规行数 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(real, 1):
        cv = "—" if r["convalidated"] is None else ("true" if r["convalidated"] else "false")
        vio = "—" if r["violations"] is None else str(r["violations"])
        lines.append(
            f"| {i} | `{r['table']}` | {r['kind']} | `{r['constraint']}` | "
            f"{'是' if r['constraint_exists'] else '否'} | {cv} | {vio} | {r['verdict']} |"
        )

    lines += [
        "",
        "## 二、「一致」半边：本行 `project_id` 必须等于父行的 `project_id`",
        "",
        "「真实」全绿不等于保护完备：本行写 `proj-beta`、父行在 `proj-alpha`，两个项目",
        "都真实、组织也一致，三列外键会一路放行。这 10 行就是堵这个洞的。",
        "",
        "| # | 表 | 父表 | 目标约束 | convalidated | 违规行数（父行缺失 ∨ 归属不同） | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(consist, 1):
        cv = "—" if r["convalidated"] is None else ("true" if r["convalidated"] else "false")
        vio = "—" if r["violations"] is None else str(r["violations"])
        lines.append(
            f"| {i} | `{r['table']}` | `{r['parent']}` | `{r['constraint']}` | "
            f"{cv} | {vio} | {r['verdict']} |"
        )

    lines.append("")
    missing = [r for r in consist if not r["pass"]]
    if missing:
        lines += [
            f"> 「一致」半边有 **{len(missing)}** 行未闭环。这一节不允许被上面的总数掩盖：",
            "> 「真实」18/18 只说明目标项目存在，不说明父子归属相同。",
            "",
        ]
    lines.append("")
    if q.get("available"):
        lines += [
            f"**隔离区占比**（#25 防垃圾桶）：projects 共 {q['project_total']} 行，"
            f"隔离项目 {q['quarantine_projects']} 个，比值 {q['ratio']}。",
            "该比值只应随时间下降；持续上升须触发复审，不得当作正常态。",
            "",
        ]
    lines += [
        "> 本台账**不接受人工填写**。任一行为 `待验证 PENDING_VALIDATE` 时，整体即未闭环；",
        "> 不得以「迁移已执行」「约束已创建」替代该项判定（D-10.1 四条不可接受结果）。",
        "",
    ]
    return "\n".join(lines)


async def run(db: str, port: int, host: str = "localhost", write: bool = True) -> dict:
    conn = await asyncpg.connect(
        host=host, port=port, user="postgres", password="replay_super_pw", database=db
    )
    try:
        rows = await collect(conn)
        q = await quarantine_stats(conn)
        server = await conn.fetchval("SHOW server_version")
    finally:
        await conn.close()

    import datetime as _dt

    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {"db": db, "host": host, "port": port, "ts": ts, "server": server}
    result = {
        "meta": meta,
        "rows": rows,
        "quarantine": q,
        "passed": sum(1 for r in rows if r["pass"]),
        "total": len(rows),
        "closed": all(r["pass"] for r in rows),
    }
    if write:
        OUT_MD.write_text(render_md(rows, q, meta), encoding="utf-8")
        OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ─────────────────────────── 自检：证明台账不是恒真 ───────────────────────────

PENDING = REPO / "data/migrations/pending/feat-ownership"

#: 参与自检的迁移，按**应用顺序**排列：(文件名 glob, 模块名)。
#: 台账从 14 行涨到 18 行是因为 `batch2` 补了三张表、`batch3` 补了 `episodes` 的归属外键，
#: 所以自检必须同时应用两批，否则 pending 场景只会有 14 条待验证。
SELFTEST_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("*_ownership_fk_backfill.py", "ownership_fk_a9b8"),
    ("*_ownership_fk_batch2.py", "ownership_fk_batch2"),
    ("*_ownership_fk_episodes.py", "ownership_fk_batch3"),
    ("*_ownership_consistency_fk.py", "ownership_fk_batch4"),
)


def _load(path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 必须先登记再 exec：dataclass 解析注解会回查 sys.modules[cls.__module__]，
    # 未登记时拿到 None 而抛 AttributeError（a9b8 当年的原坑）。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_migrations() -> list:
    """按应用顺序加载迁移体。

    一律读 `pending/feat-ownership/`，不读 alembic 的 `versions/`：pending 是这批迁移
    的登记处，versions 里的是副本。读副本会把「副本漂移」变成自检看不见的事。
    """
    return [_load(next(PENDING.glob(pattern)), name) for pattern, name in SELFTEST_MIGRATIONS]


def ownership_fks(mods) -> list[tuple[str, str]]:
    """把各批迁移的外键拼成一份全量清单。

    刻意**不写条数**：条数随批次增长，写进注释就会在下一批变成假陈述
    （本轮已从 18 涨到 28）。要条数就 `len(ownership_fks(mods))`。
    """
    return [fk for m in mods for fk in m.OWNERSHIP_FKS]


async def _recreate(db: str, template: str, port: int) -> str:
    """克隆模板库并把归属五层迁移回退到激活前基线，返回回退说明。

    为什么不能直接用模板库当「未迁移」场景
    --------------------------------------
    自检的 `ledger_nomig` 场景要求「18 条约束一条都不存在」。但模板库
    `ai_film_industrial` 已经升到 `e3f4a5b6c7d8`，归属约束全都在，且含指向隔离项目的
    行。直接克隆会让 nomig 场景实测 14 条约束 → 判定 14/14，与「未迁移」完全相反；
    更隐蔽的是 `pending` 场景（约束在、未 VALIDATE）会与 `clean` 场景难以区分。
    这与验证脚本曾经踩过的坑同源：**装置假设模板停在某个版本**。故这里显式回退，
    回退的实现复用 `lab.prepare_pre_activation_baseline`，不另写一份。
    """
    admin = await asyncpg.connect(
        host="localhost", port=port, user="postgres",
        password="replay_super_pw", database="postgres",
    )
    try:
        await lab.fresh_clone(admin, db, template)
    finally:
        await admin.close()

    conn = await asyncpg.connect(
        host="localhost", port=port, user="postgres",
        password="replay_super_pw", database=db,
    )
    try:
        purge, _order, checks = await lab.prepare_pre_activation_baseline(conn, PENDING)
    finally:
        await conn.close()
    if not all(v.endswith("=0") for v in checks):
        raise RuntimeError(f"自检基线回退未生效，判定不可信：{checks}")
    return purge


async def _drop(db: str, port: int) -> None:
    conn = await asyncpg.connect(
        host="localhost", port=port, user="postgres",
        password="replay_super_pw", database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}"')
    finally:
        await conn.close()


# 自检场景：同一脚本、不同库状态，判定必须不同。
# 旧版只有三种状态（未迁移 / 全验证 / 一张脏），缺「约束全建但一个都没 VALIDATE」——
# 而这恰恰是迁移刚激活后的真实形态，也是「约束存在 ≠ 不变量成立」最容易被误读的形态：
# 18 条约束都在、`NOT VALID` 也在，若台账把「存在」当「闭环」，这一态会被判 18/18。
SELFTEST_SCENARIOS = (
    # db              migrate  validate          dirty           passed pending  fail_rows
    dict(db="ledger_nomig", migrate=False, validate="none", dirty_mode="none",
         passed=0, pending=0, fail_rows=None),
    # pending：约束全建、一条都没 VALIDATE。所有行（含「一致」半边）都必须
    # 落在「待验证」而不是 PASS——这是「存在 ≠ 闭环」最容易翻车的那一态。
    dict(db="ledger_pending", migrate=True, validate="none", dirty_mode="none",
         passed=0, pending="all", fail_rows=None),
    dict(db="ledger_clean", migrate=True, validate="all", dirty_mode="none",
         passed="all", pending=0, fail_rows=None),
    # 只让 asset_versions 一张脏：验证台账能精确到「这一张」，而不是红灯一片。
    dict(db="ledger_dirty", migrate=True, validate="all_but_av", dirty_mode="av",
         passed="all_minus_1", pending=0,
         fail_rows=(("asset_service.asset_versions",
                     "fk_asset_versions_project_tenant_org"),)),
    # 「真实」半边完全成立、只有「一致」半边被破坏。这一态是旧版自检完全没有的：
    # 没有它，「一致 10 行」既没有被证明能通过，也没有被证明能失败——
    # 而一个永远不 FAIL 的检查等于不存在。
    dict(db="ledger_inconsistent", migrate=True, validate="all_tolerant",
         dirty_mode="inconsistent", passed="all_minus_1", pending=0,
         fail_rows=(("asset_service.qc_reports",
                     "fk_qc_reports_asset_version_project"),)),
)


async def _apply_validation(conn: asyncpg.Connection, fks, mode: str) -> None:
    """按场景决定 VALIDATE 的范围。mode 只表达「验到哪一步」，不掺业务语义。"""
    if mode == "none":
        return
    for table, name in fks:
        if mode == "all_but_av" and name == "fk_asset_versions_project_tenant_org":
            continue  # 留一张不订正：验证台账能否精确定位到这一张
        try:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        except asyncpg.PostgresError:
            # `all_tolerant`：存量存在违规时 VALIDATE 必然失败，这不是场景搭建失误，
            # 而是该场景要的形态（「约束已建但验证不过」）。其它模式仍必须抛。
            if mode != "all_tolerant":
                raise


async def selftest(port: int, template: str) -> int:
    mods = load_migrations()
    fks = ownership_fks(mods)
    results = []
    try:
        for sc in SELFTEST_SCENARIOS:
            db = sc["db"]
            await _recreate(db, template, port)
            conn = await asyncpg.connect(
                host="localhost", port=port, user="postgres",
                password="replay_super_pw", database=db,
            )
            try:
                mode = sc["dirty_mode"]
                if sc["migrate"]:
                    if mode == "av":
                        await seed_dirty_pre_fk(conn)
                    elif mode == "inconsistent":
                        await seed_inconsistent_pre_fk(conn)
                    for m in mods:
                        for step in m.upgrade_steps():
                            await conn.execute(step.sql)
                    if mode == "av":
                        await seed_dirty_post_fk(conn)
                    elif mode == "inconsistent":
                        await seed_inconsistent_post_fk(conn)
                    await _apply_validation(conn, fks, sc["validate"])
            finally:
                await conn.close()
            r = await run(db, port, write=False)
            total = r["total"]
            pending = sum(1 for row in r["rows"] if str(row["verdict"]).startswith("待验证"))
            # 期望值允许用「相对量」表达（all / all_minus_1）：台账行数会随批次增长，
            # 把 18、28 写死在断言里，下一批就得改测试——那是让测试迁就实现。
            want_passed = (
                total if sc["passed"] == "all"
                else total - 1 if sc["passed"] == "all_minus_1"
                else sc["passed"]
            )
            want_pending = total if sc["pending"] == "all" else sc["pending"]
            got_fail = tuple(
                (str(row["table"]), str(row["constraint"]))
                for row in r["rows"] if not row["pass"]
            )
            results.append(
                (db, r["passed"], total, r["closed"], pending,
                 got_fail, want_passed, want_pending, sc["fail_rows"])
            )
    finally:
        for sc in SELFTEST_SCENARIOS:
            await _drop(sc["db"], port)

    print("\n台账自检（同一脚本、多种库状态，判定必须不同）")
    print("-" * 92)
    ok = True
    for (db, passed, total, closed, pending, got_fail,
         want_passed, want_pending, want_fail) in results:
        good = (
            passed == want_passed
            and pending == want_pending
            and closed == (want_passed == total)
        )
        if want_fail is not None:
            # 只比数量是不够的：27/28 也可能是别的行在红。必须比**是哪一行**，
            # 否则「台账能精确定位」这句话没有被验证过。
            good = good and got_fail == want_fail
        ok = ok and good
        detail = "" if want_fail is None else f"  失败行={got_fail or '无'}"
        print(
            f"[{'OK  ' if good else 'BAD '}] {db:<18} 闭环={closed!s:<5} "
            f"PASS {passed}/{total}（期望 {want_passed}）  "
            f"待验证 {pending}（期望 {want_pending}）{detail}"
        )
    print("-" * 92)
    print(
        "预期：\n"
        "  nomig         全未建立 → 0 PASS\n"
        "  pending       约束全建、一条都没 VALIDATE → 0 PASS 且**全部**行待验证\n"
        "  clean         全部 VALIDATE → 全绿\n"
        "  dirty         仅 asset_versions 存量违规（指向不存在的项目）→ 全绿减 1，且**恰好**那一行\n"
        "  inconsistent  仅 qc_reports 父子归属不同（两个项目都真实）→ 全绿减 1，且**恰好**那一行"
    )
    print("结论：" + (
        "台账可区分「未建立 / 待验证 / 存量违规（真实半边）/ 归属不一致（一致半边）/ 闭环」，"
        "且失败行可精确定位——不是恒真。"
        if ok else "自检失败——台账判定不可信，禁止用于验收。"))
    return 0 if ok else 1


#: 脏态夹具用的归属三元组：一个**真实存在**的组织 + 项目，配一行指向
#: **不存在**项目的资产。两类行必须分两步造，见下面两个函数的注释。
DIRTY_TENANT = "11111111-1111-1111-1111-111111111111"
DIRTY_ORG = "22222222-2222-2222-2222-222222222222"


async def seed_dirty_pre_fk(conn: asyncpg.Connection) -> None:
    """造脏行（指向不存在的项目）。**必须在归属外键建立之前**。

    外键一旦建立（哪怕 `NOT VALID`），**新插入**就会被检查——`NOT VALID` 只豁免
    历史行扫描。所以「造脏 → 建约束 → VALIDATE 失败」这个证明链只能按这个顺序搭，
    反过来根本造不出脏行。
    """
    await conn.execute(
        f"""
        INSERT INTO asset_service.asset_versions
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             tenant_id, organization_id, project_id, created_at, created_by, updated_at,
             asset_type, sha256, storage_ref, source_asset_version_ids)
        VALUES ('av-ghost', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                '{DIRTY_TENANT}', '{DIRTY_ORG}', 'ghost-project', now(), 'ledger', now(),
                'VIDEO', repeat('a', 64),
                '{{"bucket":"b","key":"av-ghost"}}'::jsonb, '[]'::jsonb)
        """
    )


async def seed_dirty_post_fk(conn: asyncpg.Connection) -> None:
    """造**合规**的组织与项目行。必须在 `a9b8` 之后——`organizations` 表由它创建。

    组织行不可省：`projects` 挂着 `fk_projects_organization_tenant`，缺组织行会让
    `projects` 自己也违规 → 台账判 16/18，而用例要证明的是「**只**精确定位到
    asset_versions 这一张」。脏要脏得单一，否则「能否精确定位」无从判断。
    """
    await conn.execute(
        f"""
        INSERT INTO {ORGS} (organization_id, tenant_id, name, created_at, created_by, updated_at)
        VALUES ('{DIRTY_ORG}', '{DIRTY_TENANT}', 'ledger-dirty-org', now(), 'ledger', now())
        """
    )
    await conn.execute(
        f"""
        INSERT INTO {PROJECTS}
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             created_at, created_by, updated_at, project_name, project_tier,
             tenant_id, organization_id)
        VALUES ('proj-1', 'Project', 2, 1, 'ACTIVE', 'project_planning',
                now(), 'ledger', now(), 'proj-1', 'B', '{DIRTY_TENANT}', '{DIRTY_ORG}')
        """
    )


async def seed_inconsistent_pre_fk(conn: asyncpg.Connection) -> None:
    """造「**真实但不一致**」的行：父行在 proj-1，子行 project 写 proj-2。

    与 `seed_dirty_pre_fk` 的区别是**它证明的是另一个命题**：
    后者造的行 `project_id` 指向不存在的项目（「真实」半边失效）；
    这里两行都指向真实且同组织的项目（「真实」半边完全成立），
    唯一不成立的是「子行 project == 父行 project」。

    必须在约束建立之前插：`NOT VALID` 只豁免历史行扫描，新写入照样受检。
    也只造**一行**：脏要脏得单一，才能验证台账是**精确定位**而不是红灯一片。
    """
    await conn.execute(
        f"""
        INSERT INTO asset_service.asset_versions
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             tenant_id, organization_id, project_id, created_at, created_by, updated_at,
             asset_type, sha256, storage_ref, source_asset_version_ids)
        VALUES ('av-consist', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                '{DIRTY_TENANT}', '{DIRTY_ORG}', 'proj-1', now(), 'ledger', now(),
                'VIDEO', repeat('b', 64),
                '{{"bucket":"b","key":"av-consist"}}'::jsonb, '[]'::jsonb)
        """
    )
    await conn.execute(
        f"""
        INSERT INTO asset_service.qc_reports
            (qc_report_id, asset_version_id, tenant_id, organization_id, project_id,
             quality_profile_version, detector_versions, findings, created_by,
             created_at, updated_at)
        VALUES ('qc-cross', 'av-consist', '{DIRTY_TENANT}', '{DIRTY_ORG}', 'proj-2',
                'qp-1', '[]'::jsonb, '[]'::jsonb, 'ledger', now(), now())
        """
    )


async def seed_inconsistent_post_fk(conn: asyncpg.Connection) -> None:
    """造组织与**两个**真实项目。必须在 `a9b8` 之后。

    两个项目缺一不可：只造一个时，「子行 project 与父行不同」根本写不出来，
    用例会退化成 `seed_dirty_pre_fk` 的同义重复。
    """
    await conn.execute(
        f"""
        INSERT INTO {ORGS} (organization_id, tenant_id, name, created_at, created_by, updated_at)
        VALUES ('{DIRTY_ORG}', '{DIRTY_TENANT}', 'ledger-consist-org', now(), 'ledger', now())
        """
    )
    for pid in ("proj-1", "proj-2"):
        await conn.execute(
            f"""
            INSERT INTO {PROJECTS}
                (entity_id, entity_type, schema_version, entity_version, status, owner_module,
                 created_at, created_by, updated_at, project_name, project_tier,
                 tenant_id, organization_id)
            VALUES ('{pid}', 'Project', 2, 1, 'ACTIVE', 'project_planning',
                    now(), 'ledger', now(), '{pid}', 'B', '{DIRTY_TENANT}', '{DIRTY_ORG}')
            """
        )


async def apply_validate(db: str, port: int, host: str) -> list[tuple[str, str, str]]:
    """对处于「待验证」的行执行 `VALIDATE CONSTRAINT`，逐行报告结果。

    为什么把 VALIDATE 做成脚本动作而不是手敲 SQL
    --------------------------------------------
    `VALIDATE` 是闭环的**唯一**动作（D-10.1 拒绝把「约束已建」当闭环）。手敲有三个
    退化路径，本脚本都用「逐行执行 + 逐行报告 + 失败即抛」堵住：
      1. 漏行——只验了印象深刻的两三张；
      2. 失败后当成功——`23503` 被当成「已验证过」；
      3. 用 `NOT VALID` 长期顶着——台账会一直红，红着久了就没人看。
    这里**不做**任何「失败就跳过」的容错：存量脏行本来就该让 VALIDATE 失败，
    订正走例外工单，不由台账代劳。
    """
    conn = await asyncpg.connect(
        host=host, port=port, user="postgres",
        password="replay_super_pw", database=db,
    )
    out: list[tuple[str, str, str]] = []
    try:
        for row in ledger_rows():
            state = await conn.fetchrow(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = $1 AND conrelid = $2::regclass",
                row.constraint, row.table,
            )
            if state is None:
                out.append((row.table, "SKIP", "约束不存在，先执行迁移"))
                continue
            if state["convalidated"]:
                out.append((row.table, "DONE", "已闭环，无需重复验证"))
                continue
            try:
                await conn.execute(
                    f"ALTER TABLE {row.table} VALIDATE CONSTRAINT {row.constraint}"
                )
                out.append((row.table, "OK", "VALIDATE 通过"))
            except asyncpg.PostgresError as exc:
                # 存量脏行 → 23503。如实登记并继续下一行，最后由退出码体现失败。
                out.append((row.table, "FAIL", f"{exc.sqlstate}: {str(exc).splitlines()[0][:100]}"))
    finally:
        await conn.close()
    return out


async def main_async(args: argparse.Namespace) -> int:
    if args.selftest:
        return await selftest(args.port, args.template)

    if args.validate:
        print("\n执行 VALIDATE CONSTRAINT（逐行）")
        print("-" * 80)
        res = await apply_validate(args.db, args.port, args.host)
        for table, state, detail in res:
            print(f"[{state:<4}] {table:<48} {detail}")
        print("-" * 80)

    r = await run(args.db, args.port, args.host)
    passed, total = r["passed"], r["total"]
    verdict = f"PASS（{total}/{total} 闭环）" if r["closed"] else f"未通过（{passed}/{total}）"

    print(f"\n写入 {OUT_MD}")
    print(f"写入 {OUT_JSON}")
    print(f"\n整体判定：{verdict}")
    for row in r["rows"]:
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"  [{mark}] {row['table']:<48} {row['verdict']}")
    return 0 if r["closed"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="18 行归属闭环验收台账")
    ap.add_argument("--db", default="ai_film_industrial")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--template", default="ai_film_industrial", help="自检时用作克隆模板")
    ap.add_argument("--selftest", action="store_true", help="三种库各跑一次，证明可 FAIL")
    ap.add_argument(
        "--validate", action="store_true",
        help="先对未闭环的行执行 VALIDATE CONSTRAINT，再出台账（闭环动作，一次性）",
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
