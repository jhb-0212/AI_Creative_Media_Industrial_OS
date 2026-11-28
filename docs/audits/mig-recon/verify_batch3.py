"""验证 `episodes` 归属外键（g7b8c9d0e1f2）的真实行为。

证明的命题
----------
`project_planning.episodes` 补了三列归属外键之后：

1. **缺口可复现**：约束建立之前，可以把 `project_id` 写成不存在的项目；
2. **约束不误伤**：真实项目 + 同租户组织可以正常插入——没有这条对照，后面的
   `23503` 可能是「租户不对」「列可空」等任意原因造成的，看不出是归属外键在拦；
3. **新写入被拦**：约束建立之后（哪怕 `NOT VALID`），指向不存在的项目 `23503`；
4. **VALIDATE 不是恒真**：存量脏行会让 `VALIDATE CONSTRAINT` 以 `23503` 失败，
   所以台账的 PASS 确实扫过数据，而不是只看了约束是否存在；
5. **降级回到原状**：删约束后同样的脏写入又能成功——语义可逆，不是单向门。

为什么基线要「回退到 f8a9」
---------------------------
本迁移的增量前提是归属栈前六层（到 `f8a9`）已应用、批次三未建。模板库只可能停在一个状态，
把它此刻的偶然位置当前提，下一批一上就失效——上一轮 `verify_batch2` 正是这样
坏掉的。所以这里显式 `lab.rewind_to(f8a9b0c1d2e3)`，脚本表达的是**被测命题**的前提。

为什么不硬编码夹具项目
----------------------
真实项目三元组从库里现取（`status='ACTIVE'` 的第一行），而不是写死 `proj-1`。
写死的 id 一旦在某次清库后不存在，`23503` 的语义就会从「归属外键拦截」漂移成
「夹具缺失」——两种失败长得一模一样，而报错信息只报外键名。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import asyncpg
import lab

PROBE_DB = "t3b3_lab"
TABLE = "project_planning.episodes"
FK = "fk_episodes_project_tenant_org"

#: 本迁移的增量前提：这一层已应用，其后各层要先撤掉再测。
KEEP_AFTER = "f8a9b0c1d2e3"

REC = lab.Recorder()
rec = REC.rec


def ep_insert(eid: str, ref: str, project: str, tenant: str, org: str) -> str:
    """造一行 episodes。列全部显式给出：该表无默认值，缺列会以 23502 失败，
    而 23502 会被误读成「约束在拦」。"""
    return (
        f"INSERT INTO {TABLE} (entity_id, entity_type, schema_version, entity_version, "
        f"status, owner_module, created_at, created_by, updated_at, project_id, "
        f"episode_ref, title, tenant_id, organization_id) VALUES "
        f"('{eid}', 'Episode', 2, 1, 'DRAFT', 'project_planning', now(), 'b3', now(), "
        f"'{project}', '{ref}', 'ep-{ref}', '{tenant}', '{org}')"
    )


def fk_exists_sql() -> str:
    return (
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
        f"'{FK}' AND conrelid = '{TABLE}'::regclass)"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=lab.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    ap.add_argument("--user", default=lab.DEFAULT_USER)
    ap.add_argument("--password", default=lab.DEFAULT_PASSWORD)
    ap.add_argument("--template", default=lab.DEFAULT_TEMPLATE)
    ap.add_argument("--migration", required=True)
    ap.add_argument("--versions-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    if not args.versions_dir:
        args.versions_dir = str(Path(args.migration).parent)

    mig = lab.load_migration(args.migration, "mig_batch3")

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, PROBE_DB, args.template)
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, PROBE_DB)
    try:
        # ---------- 基线：回退到本迁移的增量前提 ----------
        removed = await lab.rewind_to(conn, args.versions_dir, KEEP_AFTER)
        rec(
            "P0",
            "基线",
            f"回退到 {KEEP_AFTER} 已应用的状态（撤掉 {len(removed)} 层）",
            True,
            "/".join(removed) if removed else "模板已在该状态，无需回退",
        )

        exists = await conn.fetchval(fk_exists_sql())
        rec(
            "P1",
            "前提",
            "基线里 episodes 归属外键尚不存在（否则测的不是本迁移）",
            not exists,
            f"exists={exists}",
        )
        if exists:
            raise RuntimeError("前提不成立：基线已含 episodes 归属外键")

        # ---------- 真实项目三元组（从库里现取，不硬编码） ----------
        proj = await conn.fetchrow(
            "SELECT entity_id, tenant_id::text AS tenant, organization_id::text AS org "
            "FROM project_planning.projects WHERE status = 'ACTIVE' ORDER BY entity_id LIMIT 1"
        )
        rec(
            "P2",
            "前提",
            "库里取到一行 ACTIVE 项目作为对照",
            proj is not None,
            f"{proj['entity_id']} / {proj['tenant'][-4:]} / {proj['org'][-4:]}" if proj else "无",
        )
        if proj is None:
            raise RuntimeError("没有 ACTIVE 项目，无法构造对照行")

        pid, tenant, org = proj["entity_id"], proj["tenant"], proj["org"]

        # ---------- 对照 + 缺口可复现 ----------
        ok_g, d_g = await lab.case(
            conn, [], ep_insert("b3-ok", "ep-ok", pid, tenant, org), expect_changed=1
        )
        rec("G1", "对照", "真实项目 + 同租户组织可正常插入（约束未建立前）", ok_g, d_g)

        ok_c, d_c = await lab.case(
            conn,
            [],
            ep_insert("b3-ghost", "ep-ghost", "ghost-project", tenant, org),
            expect_changed=1,
        )
        rec("C1", "缺口", "建约束前：episodes 可指向不存在的项目", ok_c, d_c)

        # ---------- 升级 ----------
        n = await lab.apply_steps(conn, lab.upgrade_sqls(mig))
        rec("U1", "升级", f"执行 upgrade_steps()（{n} 步）", True, "无异常")

        row = await conn.fetchrow("SELECT convalidated FROM pg_constraint WHERE conname = $1", FK)
        rec(
            "S1",
            "结构",
            "三列外键已建且为 NOT VALID（存量未扫）",
            row is not None and row["convalidated"] is False,
            f"exists={row is not None} convalidated={row['convalidated'] if row else '-'}",
        )

        # ---------- 新写入被拦，且不误伤 ----------
        ok_n, d_n = await lab.case(
            conn,
            [],
            ep_insert("b3-new", "ep-new", "ghost-project", tenant, org),
            expect_state="23503",
        )
        rec("N1", "拦截", "约束建立后：指向不存在的项目一律 23503", ok_n, d_n)

        ok_n2, d_n2 = await lab.case(
            conn, [], ep_insert("b3-ok2", "ep-ok2", pid, tenant, org), expect_changed=1
        )
        rec("N2", "拦截", "同一约束不误伤真实项目（否则 N1 的 23503 是别的原因）", ok_n2, d_n2)

        # ---------- VALIDATE 非恒真 ----------
        ok_v, d_v = await lab.case(
            conn, [], f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {FK}", expect_state="23503"
        )
        rec("V1", "验证", "有存量脏行时 VALIDATE 必须 23503（证明它会真扫数据）", ok_v, d_v)

        # 清脏：`session_replication_role=replica` 关掉可能存在的守卫。
        # 本表虽无不可变守卫，但夹具还原不应依赖「这张表恰好没守卫」。
        await conn.execute("SET session_replication_role = replica")
        try:
            await conn.execute(f"DELETE FROM {TABLE} WHERE entity_id = 'b3-ghost'")
        finally:
            await conn.execute("SET session_replication_role = origin")

        try:
            async with conn.transaction():
                await conn.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {FK}")
            rec("V3", "验证", "清掉脏行后 VALIDATE 通过（成功路径可达）", True, "VALIDATE OK")
        except asyncpg.PostgresError as exc:
            rec(
                "V3",
                "验证",
                "清掉脏行后 VALIDATE 通过（成功路径可达）",
                False,
                f"{exc.sqlstate}: {str(exc).splitlines()[0][:80]}",
            )

        # ---------- 幂等 ----------
        await lab.apply_steps(conn, lab.upgrade_sqls(mig))
        cnt = await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conname = $1", FK)
        rec("I1", "幂等", "重复 upgrade 不重复建约束", cnt == 1, f"count={cnt}")

        # ---------- 降级 ----------
        n = await lab.apply_steps(conn, lab._downgrade_sqls(mig))
        rec("D1", "降级", f"执行 downgrade_steps()（{n} 步）", True, "无异常")
        gone = await conn.fetchval(fk_exists_sql())
        rec("D2", "降级", "外键已删净", not gone, f"exists={gone}")

        ok_r, d_r = await lab.case(
            conn,
            [],
            ep_insert("b3-after-down", "ep-ad", "ghost-project", tenant, org),
            expect_changed=1,
        )
        rec("D3", "降级", "降级后同一脏写入恢复为可成功（语义可逆）", ok_r, d_r)

    finally:
        await conn.close()
        admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await lab.drop_lab(admin, PROBE_DB)
        finally:
            await admin.close()

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent  # noqa: ASYNC240 — CLI 启动期一次性路径解析，非并发热路径
    ts = REC.write_report(
        out_dir / "verify-batch3.md",
        out_dir / "verify-batch3.json",
        title="归属外键第三批（g7b8c9d0e1f2 / episodes）验证结果",
        meta=[
            f"目标：`{args.host}:{args.port}` 克隆库 `{PROBE_DB}`（模板 `{args.template}`），用完即删",
            f"基线：克隆后回退到 `{KEEP_AFTER}` 已应用（撤掉批次三），P0 记录撤了哪几层",
            "对照行：真实项目三元组从库中现取，不硬编码（夹具失效会让 23503 语义漂移）",
        ],
        footer=[
            "## 口径提醒",
            "",
            "- `G1`/`C1` 断言写入**成功**，`N1` 断言 `23503`，`N2` 断言真实项目仍可写。",
            "  只报 N1 不报 N2，「拦住了」也可能是「什么都写不进去」。",
            "- `V1` 断言 `VALIDATE` **失败**：若它通过，说明 VALIDATE 没扫存量，台账 PASS 是假的。",
            "- `D3` 断言脏写入恢复成功：证明 D2 删的确实是这条约束，而不是删了个别的。",
            "- 三列外键只保「目标真实 + 租户组织一致」。对 `episodes` 而言父行就是 `projects`，",
            "  故此处「一致」与「存在」等价；对 `qc_reports` 那类父行不同的表仍不等价（I-1 未闭合半边）。",
        ],
    )
    print(f"[{ts}] 写入 {out_dir / 'verify-batch3.md'}")
    print(f"\n批次三验证：{REC.passed}/{REC.total} 通过")
    return 0 if REC.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
