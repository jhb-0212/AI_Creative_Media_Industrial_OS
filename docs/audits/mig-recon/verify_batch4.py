"""验证归属「一致」外键（h8c9d0e1f2a3）的真实行为。

证明的命题
----------
1. **缺口可复现**：约束建立之前，把 `qc_reports.project_id` 写成**另一个真实项目**
   （同租户同组织）也能插入成功——三列外键完全放行，因为两个项目都真实。
   这条是整批迁移的存在理由：它把「I-1 缺的不是真实、是一致」变成可执行的断言。
2. **对照**：项目一致时可正常插入。没有这条，后面的 `23503` 可能是「资产不存在」
   「租户不对」等任意原因造成的，看不出是**一致性**在拦。
3. **新写入被拦**：约束建立后（哪怕 `NOT VALID`），跨项目挂载 `23503`。
4. **弱键被替换而不是叠加**：单列外键 `qc_reports_asset_version_id_fkey` 必须消失。
   两条键同时存在时，弱的那条会让人以为「只需要它」，而它只保父行存在。
5. **VALIDATE 不是恒真**：存量中「真实但不一致」的行会让 `VALIDATE` 以 `23503` 失败，
   证明台账的 PASS 真扫过数据。
6. **降级对称**：删约束后同一跨项目写入恢复成功，且单列外键回到原位。
7. **与闸门一致**：升级后 `verify_ownership_consistency` 不报 `NO_FK` / `FK_NOT_COMPOSITE`。

为什么基线要「回退到 g7b8」
---------------------------
本迁移的增量前提是归属栈前七层已应用、批次四未建。模板库只可能停在一个状态，
把它此刻的位置当前提，下一批一上就失效——`verify_batch2` 就这样坏过一次。
所以显式 `lab.rewind_to(g7b8c9d0e1f2)`，脚本表达的是**被测命题**的前提。

为什么不断言「重复 upgrade 幂等」
--------------------------------
`ADD CONSTRAINT` 没有 `IF NOT EXISTS`，重复执行必然 `42710`。用触发器或 DO 块
把它包装成「静默成功」只会掩盖一个更有用的事实：**约束确实唯一存在**。
所以这里反过来断言「重复执行必须以 42710 失败」——失败是设计，不是缺陷。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import asyncpg
import lab
import verify_ownership_consistency as consistency

PROBE_DB = "t3b4_lab"
KEEP_AFTER = "g7b8c9d0e1f2"

QC = "asset_service.qc_reports"
AV = "asset_service.asset_versions"
COMPOSITE_FK = "fk_qc_reports_asset_version_project"
WEAK_FK = "qc_reports_asset_version_id_fkey"
PARENT_UQ = "uq_asset_versions_entity_project_tenant_org"

REC = lab.Recorder()
rec = REC.rec


def av_insert(eid: str, project: str, tenant: str, org: str) -> str:
    """`storage_ref` / `source_asset_version_ids` 是 **jsonb** 而非 text。

    第一版按 text 写 `'s3://...'`，得到 `22P02 invalid input syntax for type json`——
    这个错误看起来像夹具写错，实际暴露的是「按列名猜类型」这个坏习惯：
    `storage_ref` 的名字像字符串，schema 里却是 jsonb。夹具必须按实际类型构造。
    """
    return (
        f"INSERT INTO {AV} (entity_id, entity_type, schema_version, entity_version, status, "
        f"owner_module, tenant_id, created_at, created_by, updated_at, asset_type, sha256, "
        f"storage_ref, source_asset_version_ids, organization_id, project_id) VALUES "
        f"('{eid}', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service', '{tenant}', now(), "
        f"'b4', now(), 'video', 'deadbeef', '{{\"uri\": \"s3://b4/{eid}\"}}', '[]', "
        f"'{org}', '{project}')"
    )


def qc_insert(rid: str, av: str, project: str, tenant: str, org: str) -> str:
    return (
        f"INSERT INTO {QC} (qc_report_id, asset_version_id, tenant_id, quality_profile_version, "
        f"detector_versions, findings, created_by, created_at, updated_at, organization_id, "
        f"project_id) VALUES "
        f"('{rid}', '{av}', '{tenant}', 'qp-1', '{{}}', '{{}}', 'b4', now(), now(), "
        f"'{org}', '{project}')"
    )


async def two_projects(conn: asyncpg.Connection) -> tuple[asyncpg.Record, asyncpg.Record]:
    """取同租户同组织下的两个 ACTIVE 项目。

    必须真取，不能写死：两个写死的 id 一旦在清库后不存在，`23503` 的语义就会
    从「跨项目被拦」漂移成「项目不存在被拦」——两种失败长得一模一样，
    而报错只给一个外键名。本批要证明的恰恰是*两者都真实*时仍然被拦。
    """
    rows = await conn.fetch(
        "SELECT entity_id, tenant_id::text AS tenant, organization_id::text AS org "
        "FROM project_planning.projects WHERE status = 'ACTIVE' "
        "ORDER BY entity_id LIMIT 2"
    )
    if len(rows) < 2:
        raise RuntimeError(f"需要 ≥2 个 ACTIVE 项目才能构造跨项目用例，实得 {len(rows)}")
    if len({(r["tenant"], r["org"]) for r in rows}) != 1:
        raise RuntimeError("两个项目的租户/组织不同，三列外键会先拦，测不到一致性")
    return rows[0], rows[1]


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

    mig = lab.load_migration(args.migration, "mig_batch4")

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, PROBE_DB, args.template)
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, PROBE_DB)
    try:
        removed = await lab.rewind_to(conn, args.versions_dir, KEEP_AFTER)
        rec("P0", "基线", f"回退到 {KEEP_AFTER} 已应用（撤掉 {len(removed)} 层）",
            True, "/".join(removed) if removed else "模板已在该状态")

        pre = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname IN ($1, $2)", COMPOSITE_FK, PARENT_UQ
        )
        rec("P1", "前提", "基线里批次四的约束均不存在（否则测的不是本迁移）",
            pre == 0, f"命中 {pre} 个")
        if pre != 0:
            raise RuntimeError("前提不成立：基线已含批次四约束")

        pa, pb = await two_projects(conn)
        pa_id, pb_id = pa["entity_id"], pb["entity_id"]
        tenant, org = pa["tenant"], pa["org"]
        rec("P2", "前提", "取到同组织下两个真实项目", True, f"A={pa_id} B={pb_id} org={org[-4:]}")

        # 资产版本归属 pa_id —— 现有弱单列外键只要求「父行存在」，不管 project
        ok_a, d_a = await lab.case(conn, [], av_insert("b4-av", pa_id, tenant, org), expect_changed=1)
        rec("P3", "前提", "插入一行属于 pa_id 的资产版本", ok_a, d_a)

        # ---------- 对照 + 缺口可复现 ----------
        ok_g, d_g = await lab.case(
            conn, [], qc_insert("b4-consistent", "b4-av", pa_id, tenant, org), expect_changed=1
        )
        rec("G1", "对照", "项目一致时可正常插入（约束未建前）", ok_g, d_g)

        ok_c, d_c = await lab.case(
            conn, [], qc_insert("b4-cross", "b4-av", pb_id, tenant, org), expect_changed=1
        )
        rec("C1", "缺口", "建约束前：报告挂在 pa_id 的资产上、项目写成 pb_id，依然插入成功",
            ok_c, d_c)

        # ---------- 升级 ----------
        n = await lab.apply_steps(conn, lab.upgrade_sqls(mig))
        rec("U1", "升级", f"执行 upgrade_steps()（{n} 步）", True, "无异常")

        st = await conn.fetchrow(
            "SELECT convalidated FROM pg_constraint WHERE conname = $1 AND conrelid = $2::regclass",
            COMPOSITE_FK, QC,
        )
        rec("S1", "结构", "复合外键已建且为 NOT VALID（存量未扫）",
            st is not None and st["convalidated"] is False,
            f"exists={st is not None} convalidated={st['convalidated'] if st else '-'}")

        weak = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = $1", WEAK_FK
        )
        rec("S2", "结构", "被取代的单列外键已删除（不是两条键并存）",
            weak == 0, f"count={weak}")

        uq = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = $1 AND contype = 'u'", PARENT_UQ
        )
        rec("S3", "结构", "父表四列唯一键已建（复合外键的可引用目标）",
            uq == 1, f"count={uq}")

        # ---------- 新写入被拦，且不误伤 ----------
        ok_n, d_n = await lab.case(
            conn, [], qc_insert("b4-new-cross", "b4-av", pb_id, tenant, org), expect_state="23503"
        )
        rec("N1", "拦截", "约束建立后：跨项目挂载一律 23503", ok_n, d_n)

        ok_n2, d_n2 = await lab.case(
            conn, [], qc_insert("b4-new-ok", "b4-av", pa_id, tenant, org), expect_changed=1
        )
        rec("N2", "拦截", "同一约束不误伤项目一致的写入（否则 N1 是别的原因）",
            ok_n2, d_n2)

        # ---------- VALIDATE 非恒真 ----------
        # 模板库自带一行真实存量脏数据（`qc-1`：挂在 proj-alpha 的资产上、
        # 自身 project 写成隔离项目）。它不是本脚本造的夹具——用真实存量行
        # 证明 VALIDATE 会扫数据，比自造一行更难反驳。
        stale = await conn.fetchval(
            f"""SELECT count(*) FROM {QC} t JOIN {AV} p ON p.entity_id = t.asset_version_id
                 WHERE (p.project_id, p.tenant_id, p.organization_id)
                    IS DISTINCT FROM (t.project_id, t.tenant_id, t.organization_id)"""
        )
        rec("V0", "验证", "存量中存在「真实但不一致」的行（VALIDATE 的必然后果）",
            stale >= 1, f"违规行 {stale} 条")

        ok_v, d_v = await lab.case(
            conn, [], f"ALTER TABLE {QC} VALIDATE CONSTRAINT {COMPOSITE_FK}",
            expect_state="23503",
        )
        rec("V1", "验证", "有「真实但不一致」存量行时 VALIDATE 必须 23503", ok_v, d_v)

        # 清掉本批夹具 + 那行存量脏行（仅克隆库）。守卫会拦 DELETE，
        # 用 replica 关触发器——这里关的是**夹具清理**，不是生产路径。
        await conn.execute("SET session_replication_role = replica")
        try:
            await conn.execute(
                f"""DELETE FROM {QC} t USING {AV} p
                     WHERE p.entity_id = t.asset_version_id
                       AND (p.project_id, p.tenant_id, p.organization_id)
                          IS DISTINCT FROM (t.project_id, t.tenant_id, t.organization_id)"""
            )
            await conn.execute(f"DELETE FROM {QC} WHERE qc_report_id LIKE 'b4-%'")
        finally:
            await conn.execute("SET session_replication_role = origin")

        try:
            async with conn.transaction():
                await conn.execute(f"ALTER TABLE {QC} VALIDATE CONSTRAINT {COMPOSITE_FK}")
            rec("V3", "验证", "清掉脏行后 VALIDATE 通过（成功路径可达）", True, "VALIDATE OK")
        except asyncpg.PostgresError as exc:
            rec("V3", "验证", "清掉脏行后 VALIDATE 通过（成功路径可达）", False,
                f"{exc.sqlstate}: {str(exc).splitlines()[0][:80]}")

        # ---------- 重复执行必须失败（约束唯一存在） ----------
        # 第一条语句是 `ADD CONSTRAINT ... UNIQUE`，背后会建同名索引，
        # 于是重复执行报 `42P07 duplicate_table` 而不是 `42710 duplicate_object`。
        # 两个码都表示「已存在」，断言接受两者——但必须**失败**，不能静默成功。
        try:
            await conn.execute(lab.upgrade_sqls(mig)[0])
            rec("I1", "幂等", "重复 ADD CONSTRAINT 必须失败（42P07/42710）", False, "竟然成功")
        except asyncpg.PostgresError as exc:
            rec("I1", "幂等", "重复 ADD CONSTRAINT 必须失败（42P07/42710）",
                exc.sqlstate in {"42710", "42P07"}, f"{exc.sqlstate}")

        # ---------- 与闸门一致 ----------
        report = await consistency.evaluate(conn)
        blocked = report.count("NO_FK") + report.count("FK_NOT_COMPOSITE")
        rec("Q1", "闸门", "升级后一致闸门无 NO_FK / FK_NOT_COMPOSITE（DANGLING 不计）",
            blocked == 0,
            f"NO_FK={report.count('NO_FK')} FK_NOT_COMPOSITE={report.count('FK_NOT_COMPOSITE')} "
            f"PENDING={report.count('PENDING_VALIDATE')} DANGLING={report.count('DANGLING')}")

        # ---------- 降级 ----------
        n = await lab.apply_steps(conn, lab._downgrade_sqls(mig))
        rec("D1", "降级", f"执行 downgrade_steps()（{n} 步）", True, "无异常")

        left = await conn.fetchval("""
            SELECT count(*) FROM pg_constraint
             WHERE conname IN (SELECT conname FROM pg_constraint
                                WHERE conname LIKE 'fk_%_project' OR conname LIKE 'uq_%_project_tenant_org')
               AND connamespace::regnamespace::text IN
                   ('asset_service','shot_planning','qc_governance','workflow','generation_execution')
               AND conname IN ('fk_qc_reports_asset_version_project',
                               'fk_qc_review_sessions_asset_version_project',
                               'fk_qc_review_sessions_qc_report_project',
                               'fk_shot_specs_shot_revision_project',
                               'fk_shot_drafts_episode_project',
                               'fk_shot_revisions_episode_project',
                               'fk_approval_tickets_flow_project',
                               'fk_approval_steps_ticket_project',
                               'fk_generation_tasks_route_org',
                               'fk_shot_route_selections_route_org',
                               'uq_asset_versions_entity_project_tenant_org',
                               'uq_shot_revisions_entity_project_tenant_org',
                               'uq_qc_reports_id_project_tenant_org',
                               'uq_approval_flows_entity_project_tenant_org',
                               'uq_approval_tickets_entity_project_tenant_org',
                               'uq_production_routes_entity_org')
        """)
        rec("D2", "降级", "批次四的复合外键与父表唯一键已删净", left == 0, f"残留={left}")

        restored = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = $1", WEAK_FK
        )
        rec("D3", "降级", "被取代的单列外键已恢复（回退到 g7b8 时刻等价）",
            restored == 1, f"count={restored}")

        ok_r, d_r = await lab.case(
            conn, [], qc_insert("b4-after-down", "b4-av", pb_id, tenant, org), expect_changed=1
        )
        rec("D4", "降级", "降级后同一跨项目写入恢复成功（语义可逆）", ok_r, d_r)

    finally:
        await conn.close()
        admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await lab.drop_lab(admin, PROBE_DB)
        finally:
            await admin.close()

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent  # noqa: ASYNC240 — CLI 启动期一次性路径解析，非并发热路径
    ts = REC.write_report(
        out_dir / "verify-batch4.md",
        out_dir / "verify-batch4.json",
        title="归属「一致」外键（h8c9d0e1f2a3）验证结果",
        meta=[
            f"目标：`{args.host}:{args.port}` 克隆库 `{PROBE_DB}`（模板 `{args.template}`），用完即删",
            f"基线：克隆后回退到 `{KEEP_AFTER}` 已应用（撤掉批次四），P0 记录撤了哪几层",
            "对照：两个真实项目从库中现取且同租户同组织——**两者都真实**才是「一致」命题的前提",
        ],
        footer=[
            "## 口径提醒",
            "",
            "- `C1` 断言跨项目写入**成功**：这是缺口的正面复现，不是缺陷复现脚本本身有 bug。",
            "  若 C1 失败，说明三列外键先拦住了，用例实为「项目不存在」，证明不了「一致」缺失。",
            "- `N2` 与 `N1` 必须成对：只有 N1 时，「全都写不进去」也能让 N1 通过。",
            "- `S2` 断言弱单列外键**已消失**：两条键并存时会掩盖「弱键保不住一致」这件事。",
            "- `V1` 断言 VALIDATE **失败**：它若通过，说明没扫存量，台账 PASS 是假的。",
            "- `I1` 断言重复执行**失败**：迁移不假装幂等——`ADD CONSTRAINT` 本就没有",
            "  `IF NOT EXISTS`，把它包装成静默成功会掩盖「约束唯一存在」这个事实。",
            "- `Q1` 把本迁移与闸门对齐：迁移建完的边，闸门必须认；不认就是两者规则漂移。",
            "- `DANGLING` 不计入 Q1：`shots` / `scenes` / `gate_instances` 表尚未创建，",
            "  属镜头域运行时未落地，无法由外键修复，已在一致闸门的报告里单列。",
        ],
    )
    print(f"[{ts}] 写入 {out_dir / 'verify-batch4.md'}")
    print(f"\n批次四验证：{REC.passed}/{REC.total} 通过")
    return 0 if REC.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
