"""验证归属外键第二批（f8a9b0c1d2e3）真实行为。

证明的命题
----------
三张「有归属列却无归属外键」的表（`qc_reports` / `shot_specs` / `approval_steps`）
在补了三条三列外键之后：

1. **缺口可复现**：约束建立之前，可以把 `project_id` 写成不存在的项目；
2. **新写入被拦**：约束建立之后（哪怕 `NOT VALID`），同样的写入 `23503`；
3. **VALIDATE 不是恒真**：存量脏行会让 `VALIDATE CONSTRAINT` 以 `23503` 失败，
   所以"台账 PASS"确实扫过数据，而不是只看了约束是否存在；
4. **降级回到原状**：删约束后同样的脏写入又能成功——语义可逆，不是单向门。

为什么不做基线回退
------------------
本迁移的前提是 `a9b8` 已应用（目标唯一键 `uq_projects_entity_tenant_org` 由它创建），
而模板库恰好就停在这个状态之后、且批次二尚未建。所以这里**直接克隆模板、不回退**：
回退会把 `projects` 与前两批约束一起拆掉，反而测不到本迁移的增量。
「不回退」在这里是正确的前提，不是又一次假设模板干净——本脚本会断言模板上
批次二的三条约束**不存在**，存在即说明前提变了，立即中止。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import asyncpg

import guard_fixtures as fx
import lab

PROBE_DB = "t3b2_lab"
TABLE_QC = "asset_service.qc_reports"
TABLE_SS = "shot_planning.shot_specs"
TABLE_AS = "workflow.approval_steps"
TICKET = "WO-BATCH2"

REC = lab.Recorder()
rec = REC.rec

#: 三表的「行标识列」。qc_reports 的主键是 qc_report_id，其余是 entity_id。
KEY = {TABLE_QC: "qc_report_id", TABLE_SS: "entity_id", TABLE_AS: "entity_id"}


def qcr_insert(rid: str, av: str, project: str) -> str:
    return (
        f"INSERT INTO {TABLE_QC} (qc_report_id, asset_version_id, tenant_id, organization_id, "
        f"project_id, quality_profile_version, detector_versions, findings, created_by, "
        f"created_at, updated_at) VALUES ('{rid}', '{av}', '{fx.TENANT}', '{fx.ORG}', "
        f"'{project}', 'qp-1', '{{}}'::jsonb, '[]'::jsonb, 'b2', now(), now())"
    )


def fk_exists_sql(table: str, name: str) -> str:
    return (
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
        f"'{name}' AND conrelid = '{table}'::regclass)"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=lab.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    ap.add_argument("--user", default=lab.DEFAULT_USER)
    ap.add_argument("--password", default=lab.DEFAULT_PASSWORD)
    ap.add_argument("--template", default=lab.DEFAULT_TEMPLATE)
    ap.add_argument("--migration", required=True)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    mig = lab.load_migration(args.migration, "mig_batch2")
    fks = dict(mig.OWNERSHIP_FKS)  # table -> constraint name

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, PROBE_DB, args.template)
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, PROBE_DB)
    try:
        # ---------- 前提 ----------
        pre = {t: await conn.fetchval(fk_exists_sql(t, n)) for t, n in fks.items()}
        ok = not any(pre.values())
        rec(
            "P1",
            "前提",
            "模板上批次二的三条外键尚不存在（否则测的不是本迁移）",
            ok,
            "; ".join(f"{t}={v}" for t, v in pre.items()),
        )
        if not ok:
            raise RuntimeError(f"前提不成立，模板已含批次二约束：{pre}")

        q = await conn.fetchval(
            "SELECT count(*) FROM project_planning.organizations WHERE organization_id = $1::uuid",
            fx.ORG,
        )
        rec("P2", "前提", "夹具组织存在（外键目标真实，否则 23503 会被误读为拦截）",
            (q or 0) >= 1, f"organizations={q}")

        # ---------- 缺口可复现 ----------
        # 约束建立之前，把 project_id 指向不存在的项目：应当**成功**，
        # 这正是「例外可以把归属改到不存在的项目」的实测证据。
        ok_c, d_c = await lab.case(
            conn, [], qcr_insert("b2-ghost-qc", "av-clean", "ghost-project"), expect_changed=1
        )
        rec("C1", "缺口", "建约束前：qc_reports 可指向不存在的项目", ok_c, d_c)
        # 另两张表同样造一行脏数据（供 V1 证明 VALIDATE 非恒真）
        for tbl, key in ((TABLE_SS, "b2-ghost-ss"), (TABLE_AS, "b2-ghost-as")):
            ok_i, d_i = await lab.case(
                conn,
                [],
                fx.insert_sql(tbl, key, project="ghost-project"),
                expect_changed=1,
            )
            rec(f"C1{key[-2:]}", "缺口", f"建约束前：{tbl} 可指向不存在的项目", ok_i, d_i)

        # ---------- 升级 ----------
        n = await lab.apply_steps(conn, mig.upgrade_steps())
        rec("U1", "升级", f"执行 upgrade_steps()（{n} 步）", True, "无异常")

        rows = await conn.fetch(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1::text[])",
            list(fks.values()),
        )
        got = {r["conname"]: r["convalidated"] for r in rows}
        rec(
            "S1",
            "结构",
            "三条三列外键已建且均为 NOT VALID（存量未扫）",
            len(got) == len(fks) and not any(got.values()),
            f"count={len(got)}/3 convalidated={sorted(set(got.values()))}",
        )

        # ---------- 新写入被拦 ----------
        nc = []
        ok_n, d_n = await lab.case(
            conn, [], qcr_insert("b2-new-qc", "av-clean", "ghost-project"), expect_state="23503"
        )
        nc.append((TABLE_QC, ok_n, d_n))
        for tbl, key in ((TABLE_SS, "b2-new-ss"), (TABLE_AS, "b2-new-as")):
            ok_n, d_n = await lab.case(
                conn, [], fx.insert_sql(tbl, key, project="ghost-project"), expect_state="23503"
            )
            nc.append((tbl, ok_n, d_n))
        rec(
            "N1",
            "拦截",
            "约束建立后：三表新写入不存在的项目一律 23503",
            all(x[1] for x in nc),
            "; ".join(f"{t.split('.')[1]}={d[:40]}" for t, _, d in nc),
        )

        # ---------- VALIDATE 非恒真 ----------
        # qc_reports 里有一行 C1 造的脏数据 → VALIDATE 必须失败。
        # 若这里「通过」，说明 VALIDATE 根本没扫存量，台账的 PASS 就是假的。
        ok_v, d_v = await lab.case(
            conn,
            [],
            f"ALTER TABLE {TABLE_QC} VALIDATE CONSTRAINT {fks[TABLE_QC]}",
            expect_state="23503",
        )
        rec("V1", "验证", "有存量脏行时 VALIDATE 必须 23503（证明它会真扫数据）", ok_v, d_v)

        # 另两张表的脏行也在，故同样应失败；随后逐表清干净再验，证明成功路径可达。
        cleanable = {TABLE_SS: "b2-ghost-ss", TABLE_AS: "b2-ghost-as"}
        cleaned = []
        for tbl, key in cleanable.items():
            ok_v2, d_v2 = await lab.case(
                conn,
                [],
                f"ALTER TABLE {tbl} VALIDATE CONSTRAINT {fks[tbl]}",
                expect_state="23503",
            )
            cleaned.append((tbl, ok_v2, d_v2))
        rec(
            "V2",
            "验证",
            "另两表的脏行同样让 VALIDATE 失败（三表都真扫）",
            all(x[1] for x in cleaned),
            "; ".join(f"{t.split('.')[1]}={d_v2[:40]}" for t, _, d_v2 in cleaned),
        )

        # 清脏：`session_replication_role=replica` 关掉不可变守卫。
        # 这一步**不是被测行为**（守卫本就禁止 DELETE，E8 已证），只是把夹具还原；
        # 若在这里走例外通道反而会混淆命题——例外按设计不管 DELETE。
        await conn.execute("SET session_replication_role = replica")
        try:
            await conn.execute(f"DELETE FROM {TABLE_QC} WHERE qc_report_id = 'b2-ghost-qc'")
            await conn.execute(f"DELETE FROM {TABLE_SS} WHERE entity_id = 'b2-ghost-ss'")
            await conn.execute(f"DELETE FROM {TABLE_AS} WHERE entity_id = 'b2-ghost-as'")
        finally:
            await conn.execute("SET session_replication_role = origin")

        vs = []
        for tbl, name in fks.items():
            # 不用 lab.case：`ALTER TABLE ... VALIDATE` 不返回行数，
            # expect_changed 会因拿不到「UPDATE n」而恒定判失败。
            try:
                async with conn.transaction():
                    await conn.execute(f"ALTER TABLE {tbl} VALIDATE CONSTRAINT {name}")
                vs.append((tbl, True, "VALIDATE OK"))
            except asyncpg.PostgresError as exc:
                vs.append((tbl, False, f"{exc.sqlstate}: {str(exc).splitlines()[0][:80]}"))
        rec(
            "V3",
            "验证",
            "清掉脏行后三表 VALIDATE 全部通过（成功路径可达）",
            all(x[1] for x in vs),
            "; ".join(f"{t.split('.')[1]}={d}" for t, _, d in vs),
        )

        # ---------- 幂等 ----------
        before = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1::text[])",
            list(fks.values()),
        )
        await lab.apply_steps(conn, mig.upgrade_steps())
        after = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1::text[])",
            list(fks.values()),
        )
        rec("I1", "幂等", "重复 upgrade 不重复建约束", before == after == len(fks),
            f"{before}→{after}")

        # ---------- 降级 ----------
        n = await lab.apply_steps(conn, mig.downgrade_steps())
        rec("D1", "降级", f"执行 downgrade_steps()（{n} 步）", True, "无异常")
        gone = {t: await conn.fetchval(fk_exists_sql(t, nm)) for t, nm in fks.items()}
        rec("D2", "降级", "三条外键已删净", not any(gone.values()),
            "; ".join(f"{t}={v}" for t, v in gone.items()))

        # 语义可逆：约束没了，脏写入又能成功——证明 D2 不是「删了个别的约束」。
        ok_r, d_r = await lab.case(
            conn, [], qcr_insert("b2-after-down", "av-clean", "ghost-project"), expect_changed=1
        )
        rec("D3", "降级", "降级后同一脏写入恢复为可成功（语义可逆）", ok_r, d_r)

    finally:
        await conn.close()
        admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await lab.drop_lab(admin, PROBE_DB)
        finally:
            await admin.close()

    total, passed = REC.total, REC.passed
    print(f"\n批次二验证：{passed}/{total} 通过")
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent
    ts = REC.write_report(
        out_dir / "verify-batch2.md",
        out_dir / "verify-batch2.json",
        title="归属外键第二批（f8a9b0c1d2e3）验证结果",
        meta=[
            f"目标：`{args.host}:{args.port}` 克隆库 `{PROBE_DB}`（模板 `{args.template}`），用完即删",
            "基线：**直接克隆模板、不回退**——本迁移的增量前提是 a9b8 已应用；"
            "P1 断言模板上批次二约束不存在，前提变了立即中止",
        ],
        footer=[
            "## 口径提醒",
            "",
            "- `C1` 与 `D3` 断言「脏写入**成功**」：这是缺口与可逆性的证据，不是缺陷复现后又消失。",
            "- `V1/V2` 断言 `VALIDATE` **失败**：若它通过，说明 VALIDATE 没扫存量，"
            "台账上的 PASS 就是假的。",
            "- `V3` 断言 `VALIDATE` 成功路径可达；清理脏行走 `session_replication_role=replica` "
            "关守卫，因为守卫本就禁止 DELETE（E8 已证），这里不是被测行为。",
            "- 三列外键只保「目标真实且组织一致」，**不保与父行的 project 一致**——"
            "后者需复合外键指向父行或触发器，不在本迁移范围。",
        ],
    )
    print(f"[{ts}] 写入 {out_dir / 'verify-batch2.md'}")
    return 0 if REC.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
