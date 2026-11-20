"""14 行归属闭环验收台账（ADR-R40 §13 D-10.1 / 附录 J.2 的唯一验收判据）。

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

REPO = Path(__file__).resolve().parents[3]
OUT_MD = Path(__file__).resolve().parent / "acceptance-ledger.md"
OUT_JSON = Path(__file__).resolve().parent / "acceptance-ledger.json"

PROJECTS = "project_planning.projects"
ORGS = "project_planning.organizations"


@dataclass(frozen=True)
class Row:
    """台账一行：一张表 + 它的目标约束 + 归属类型。"""

    table: str
    kind: str  # "A" 项目级 / "B" 组织级 / "ROOT" 归属根
    constraint: str


def ledger_rows() -> list[Row]:
    """14 行。前 13 行是域表（含 1 张组织级），第 14 行是归属根 projects。

    刻意**不含** shot_specs / approval_steps（C 派生级）：本轮不为它们建归属外键，
    把它们计入台账会让「闭环」变成假指标——为了让数字好看去建不该建的约束。
    """
    return [
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
        Row("asset_service.delivery_manifests", "A",
            "fk_delivery_manifests_project_tenant_org"),
        Row("asset_service.asset_versions", "A", "fk_asset_versions_project_tenant_org"),
        Row("qc_governance.gate_decisions", "A", "fk_gate_decisions_project_tenant_org"),
        Row("qc_governance.qc_review_sessions", "A",
            "fk_qc_review_sessions_project_tenant_org"),
        Row("workflow.approval_flows", "A", "fk_approval_flows_project_tenant_org"),
        Row("workflow.approval_tickets", "A", "fk_approval_tickets_project_tenant_org"),
        Row(PROJECTS, "ROOT", "fk_projects_organization_tenant"),
    ]


def violation_sql(row: Row) -> str:
    """按归属类型选比对对象：A 对 projects，B/ROOT 对 organizations。

    用 LEFT JOIN + IS NULL 而不是 `NOT EXISTS`，是为了让查询计划与「索引存在与否」
    无关地给出确定结果；也便于把该 SQL 直接贴进工单作为复核依据。
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
        # 引用目标也可能不存在（分支谱系缺 organizations）——此时违规行数无法计算，
        # 必须显式报「不可计算」而不是报 0：报 0 会让台账在缺目标键的库上看起来干净。
        target = PROJECTS if row.kind == "A" else ORGS
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
    overall = "PASS（14/14 闭环）" if passed == len(rows) else f"未通过（{passed}/{len(rows)}）"
    lines = [
        "# 归属闭环验收台账（机器生成，勿手改）",
        "",
        f"- 目标库：`{meta['db']}` @ `{meta['host']}:{meta['port']}`",
        f"- 采集时间：{meta['ts']}",
        f"- 判据：`PASS ⇔ 约束存在 ∧ convalidated ∧ 违规行数 = 0`（ADR-R40 §13 D-10.1 / 附录 J.2）",
        f"- **整体判定：{overall}**",
        "",
        "| # | 表 | 类型 | 目标约束 | 约束存在 | convalidated | 违规行数 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        cv = "—" if r["convalidated"] is None else ("true" if r["convalidated"] else "false")
        vio = "—" if r["violations"] is None else str(r["violations"])
        lines.append(
            f"| {i} | `{r['table']}` | {r['kind']} | `{r['constraint']}` | "
            f"{'是' if r['constraint_exists'] else '否'} | {cv} | {vio} | {r['verdict']} |"
        )
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

def load_migration():
    path = next((REPO / "data/migrations/pending/feat-ownership").glob("*_ownership_fk_backfill.py"))
    spec = importlib.util.spec_from_file_location("ownership_migration", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 需要模块已登记
    spec.loader.exec_module(mod)
    return mod


async def _recreate(db: str, template: str, port: int) -> None:
    conn = await asyncpg.connect(
        host="localhost", port=port, user="postgres",
        password="replay_super_pw", database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}"')
        await conn.execute(f'CREATE DATABASE "{db}" TEMPLATE "{template}"')
    finally:
        await conn.close()


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
# 14 条约束都在、`NOT VALID` 也在，若台账把「存在」当「闭环」，这一态会被判 14/14。
SELFTEST_SCENARIOS = (
    # db              migrate  validate       dirty  passed pending
    dict(db="ledger_nomig",   migrate=False, validate="none",       dirty=False, passed=0,  pending=0),
    dict(db="ledger_pending", migrate=True,  validate="none",       dirty=False, passed=0,  pending=14),
    dict(db="ledger_clean",   migrate=True,  validate="all",        dirty=False, passed=14, pending=0),
    dict(db="ledger_dirty",   migrate=True,  validate="all_but_av", dirty=True,  passed=13, pending=0),
)


async def _apply_validation(conn: asyncpg.Connection, fks, mode: str) -> None:
    """按场景决定 VALIDATE 的范围。mode 只表达「验到哪一步」，不掺业务语义。"""
    if mode == "none":
        return
    for table, name in fks:
        if mode == "all_but_av" and name == "fk_asset_versions_project_tenant_org":
            continue  # 留一张不订正：验证台账能否精确定位到这一张
        await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


async def selftest(port: int, template: str) -> int:
    mig = load_migration()
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
                if sc["migrate"]:
                    if sc["dirty"]:
                        await seed_dirty(conn)
                    for step in mig.upgrade_steps():
                        await conn.execute(step.sql)
                    await _apply_validation(conn, mig.OWNERSHIP_FKS, sc["validate"])
            finally:
                await conn.close()
            r = await run(db, port, write=False)
            pending = sum(1 for row in r["rows"] if str(row["verdict"]).startswith("待验证"))
            results.append((db, r["passed"], r["total"], r["closed"], pending))
    finally:
        for sc in SELFTEST_SCENARIOS:
            await _drop(sc["db"], port)

    print("\n台账自检（同一脚本、四种库状态，判定必须不同）")
    print("-" * 80)
    by_db = {sc["db"]: sc for sc in SELFTEST_SCENARIOS}
    ok = True
    for db, passed, total, closed, pending in results:
        sc = by_db[db]
        want_closed = sc["passed"] == total
        good = passed == sc["passed"] and pending == sc["pending"] and closed == want_closed
        ok = ok and good
        print(
            f"[{'OK  ' if good else 'BAD '}] {db:<14} 闭环={closed!s:<5} "
            f"PASS {passed}/{total}（期望 {sc['passed']}）  待验证 {pending}（期望 {sc['pending']}）"
        )
    print("-" * 80)
    print(
        "预期：nomig 全未建立 → 0；pending 约束全建但未 VALIDATE → 0 PASS 且 14 行待验证；\n"
        "      clean 全部 VALIDATE → 14/14；dirty 仅 asset_versions 存量违规 → 13/14（精确定位）。"
    )
    print("结论：" + ("台账可区分「未建立 / 待验证 / 存量违规 / 闭环」四种状态，不是恒真。"
                    if ok else "自检失败——台账判定不可信，禁止用于验收。"))
    return 0 if ok else 1


async def seed_dirty(conn: asyncpg.Connection) -> None:
    """造一行指向不存在项目的资产，模拟 legacy-unassigned 类存量。"""
    t1 = "11111111-1111-1111-1111-111111111111"
    o1 = "22222222-2222-2222-2222-222222222222"
    await conn.execute(
        f"""
        INSERT INTO {PROJECTS}
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             created_at, created_by, updated_at, project_name, project_tier,
             tenant_id, organization_id)
        VALUES ('proj-1', 'Project', 2, 1, 'ACTIVE', 'project_planning',
                now(), 'ledger', now(), 'proj-1', 'P1', '{t1}', '{o1}')
        """
    )
    await conn.execute(
        f"""
        INSERT INTO asset_service.asset_versions
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             tenant_id, organization_id, project_id, created_at, created_by, updated_at,
             asset_type, sha256, storage_ref, source_asset_version_ids)
        VALUES ('av-ghost', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                '{t1}', '{o1}', 'ghost-project', now(), 'ledger', now(),
                'VIDEO', repeat('a', 64),
                '{{"bucket":"b","key":"av-ghost"}}'::jsonb, '[]'::jsonb)
        """
    )


async def main_async(args: argparse.Namespace) -> int:
    if args.selftest:
        return await selftest(args.port, args.template)

    r = await run(args.db, args.port, args.host)
    passed, total = r["passed"], r["total"]
    verdict = "PASS（14/14 闭环）" if r["closed"] else f"未通过（{passed}/{total}）"

    print(f"\n写入 {OUT_MD}")
    print(f"写入 {OUT_JSON}")
    print(f"\n整体判定：{verdict}")
    for row in r["rows"]:
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"  [{mark}] {row['table']:<48} {row['verdict']}")
    return 0 if r["closed"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="14 行归属闭环验收台账")
    ap.add_argument("--db", default="ai_film_industrial")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--template", default="ai_film_industrial", help="自检时用作克隆模板")
    ap.add_argument("--selftest", action="store_true", help="三种库各跑一次，证明可 FAIL")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
