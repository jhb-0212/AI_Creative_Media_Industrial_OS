"""验证「受控例外」迁移（b0c9d8e7f6a5）真实行为。

验证原则
--------
1. 执行的是 `b0c9.upgrade_sqls()` **本体**，不是照抄一遍的 SQL。
2. 每条用例单独记录 SQLSTATE 与消息，失败即失败，不做「大概通过」。
3. 正向用例必须证明「值确实变了」，不能只看「没报错」——后者可能被
   静默的 0 行更新伪装成成功（本项目已出现过多次这类真空真陷阱）。
4. 降级后必须逐字比对 13 个守卫函数定义与升级前基线一致。

库隔离：全程只在克隆库 t3x_lab 内操作，结束即删。模板库、两分支 head、
15432、ai_film_verify、生产均不触碰。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

import lab

TENANT = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-000000000011"

ASSETS = "asset_service.asset_versions"
GATES = "qc_governance.gate_decisions"
QCR = "asset_service.qc_reports"
MAP = "project_planning.ownership_repair_map"
ALLOWS = "project_planning.ownership_repair_allows"
PROBE_ROLE = "t3x_probe"

# 记录与断言改为共用 `lab`：克隆/连接/执行/指纹这些机械动作只应有一份实现。
# 本脚本保留的只有「判定语义」——期望 P0001 还是期望影响 1 行。
REC = lab.Recorder()
results = REC.rows
rec = REC.rec
probe = lab.case
fn_hash = lab.fn_hash


def asset_insert(eid: str, project: str = "proj-1", tone: str = "a") -> str:
    return (
        f"INSERT INTO {ASSETS} (entity_id, entity_type, schema_version, entity_version, status, "
        f"owner_module, tenant_id, organization_id, project_id, created_at, created_by, updated_at, "
        f"asset_type, sha256, storage_ref, source_asset_version_ids) VALUES "
        f"('{eid}', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service', "
        f"'{TENANT}', '{ORG}', '{project}', now(), 't3x', now(), "
        f"'VIDEO', repeat('{tone}', 64), "
        f"'{{\"bucket\":\"b\",\"key\":\"{eid}\"}}'::jsonb, '[]'::jsonb)"
    )


def gate_insert(eid: str, project: str = "proj-1") -> str:
    return (
        f"INSERT INTO {GATES} (entity_id, entity_type, schema_version, entity_version, status, "
        f"owner_module, tenant_id, organization_id, project_id, created_at, created_by, updated_at, "
        f"gate_key, gate_instance_id, decision, constraint_snapshot_id, ruleset_version, "
        f"entry_criteria, required_artifacts, reviewers, reason_code, comment, evidence_refs, scope, "
        f"decided_by, decided_by_subject_type, decided_at) VALUES "
        f"('{eid}', 'GateDecision', 2, 1, 'DECIDED', 'qc_governance', "
        f"'{TENANT}', '{ORG}', '{project}', now(), 't3x', now(), "
        f"'G1', 'gi-{eid}', 'REJECTED', 'cs-{eid}', 1, 'ec', 'ra', '[]'::jsonb, 'RC', "
        f"'t3x 验证用例', '[]'::jsonb, '{{}}'::jsonb, 't3x', 'SERVICE', now())"
    )


def qcr_insert(rid: str, av: str) -> str:
    return (
        f"INSERT INTO {QCR} (qc_report_id, asset_version_id, tenant_id, organization_id, project_id, "
        f"quality_profile_version, detector_versions, findings, created_by, created_at, updated_at) "
        f"VALUES ('{rid}', '{av}', '{TENANT}', '{ORG}', 'proj-1', 'qp-1', '{{}}'::jsonb, '[]'::jsonb, "
        f"'t3x', now(), now())"
    )


def map_insert(table: str, row_key: str, ticket: str, *, new_project: str = "proj-2") -> str:
    return (
        f"INSERT INTO {MAP} (table_name, row_key, old_project_id, old_organization_id, old_tenant_id, "
        f"new_project_id, new_organization_id, new_tenant_id, reason, ticket_ref) VALUES "
        f"('{table}', '{row_key}', 'proj-1', '{ORG}', '{TENANT}', "
        f"'{new_project}', '{ORG}', '{TENANT}', '工单订正：归属事实错误，按 DE-T3X 执行', '{ticket}')"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="replay_super_pw")
    ap.add_argument("--template", default="ai_film_industrial")
    ap.add_argument("--lab", default="t3x_lab")
    ap.add_argument("--migration", required=True)
    ap.add_argument(
        "--versions-dir",
        default=None,
        help="迁移目录，默认取 --migration 所在目录；用于把克隆库回退到激活前基线",
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="报告输出目录，默认为本脚本所在目录。刻意不写进迁移目录："
        "`data/migrations/versions/` 的职责只有迁移脚本，混入产物会让 Alembic 目录即证据库",
    )
    ap.add_argument(
        "--skip-rewind",
        action="store_true",
        help="跳过基线回退。仅在库本来就在激活前基线时才可用——"
        "模板库已升到 e3f4 后若加此开关，脚本验证的将不是被测迁移",
    )
    args = ap.parse_args()

    spec = importlib.util.spec_from_file_location("mig_b0c9", args.migration)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    admin = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user, password=args.password, database="postgres"
    )
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{args.lab}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{args.lab}" TEMPLATE "{args.template}"')
        # 探针角色：非 afs_data_repair 成员，用于证明「非成员被守卫拒绝」。
        # BYPASSRLS 是**测试夹具**属性，不是产品设计：目标表启用了 RLS，
        # 普通角色会在 RLS 层被静默过滤成「UPDATE 0」，测不到守卫。
        # 加 BYPASSRLS 才能让「守卫是否拦住」成为唯一变量。
        await admin.execute(f"DROP ROLE IF EXISTS {PROBE_ROLE}")
        await admin.execute(f"CREATE ROLE {PROBE_ROLE} NOLOGIN BYPASSRLS")
        # 清掉可能残留的例外角色（上一轮测试遗留），保证基线干净。
        # 但角色可能已在其它库被正式授权（目标库完成激活之后就是这样），
        # 此时 DROP 会 DependentObjectsStillExist，由 lab.drop_role 统一吞掉。
        # 代价：本次验证覆盖的是「角色已存在」这条路径，新建路径未被覆盖；
        # E1 的幂等性另由重跑 upgrade 的用例证明。
        if not await lab.drop_role(admin, "afs_data_repair"):
            print("[note] afs_data_repair 已在其它库被授权，跳过删除（非失败）")
    finally:
        await admin.close()

    conn = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user, password=args.password, database=args.lab
    )
    try:
        # ---------- 回到激活前基线 ----------
        # 模板库在归属五层迁移激活后停在 e3f4a5b6c7d8，且已被写入指向隔离项目的
        # 行。若直接在其上重放 upgrade，测到的就不是本迁移，因此先清行、回退、
        # 断言，而不是假设模板还停在原处。
        if not args.skip_rewind:
            vd = Path(args.versions_dir) if args.versions_dir else Path(args.migration).parent
            purge, order, checks = await lab.prepare_pre_activation_baseline(conn, vd)
            ok = all(v.endswith("=0") for v in checks)
            rec(
                "B-1",
                "基线",
                f"回退归属栈至激活前基线（{len(order)} 层）",
                ok,
                f"{purge}; " + "; ".join(checks),
            )
            if not ok:
                # 基线不可信就不许继续：在有外键/有例外的混合库上跑出的「通过」
                # 与被测命题无关，比失败更危险。
                raise RuntimeError(f"基线回退未生效：{checks}")

        # ---------- 基线 ----------
        guards = [(fqn.split(".")[0], fqn.split(".")[1] + "_guard") for fqn in mig.GUARD_ORDER]
        baseline = {}
        for schema, name in guards:
            baseline[f"{schema}.{name}"] = await fn_hash(conn, schema, name)
        rec("B0", "基线", f"记录 {len(baseline)} 个守卫函数定义哈希", True, f"{len(baseline)} 个")

        # 升级前的原始拒绝行为（用于降级后与语义等价比对）
        await conn.execute(asset_insert("av-base", tone="b"))
        await conn.execute(asset_insert("av-1", tone="a"))
        await conn.execute(gate_insert("gd-base"))
        await conn.execute(qcr_insert("qr-base", "av-base"))
        # 让探针角色拥有写权限，用于证明「拒绝来自守卫而非权限」。
        # 这一步必须在 upgrade 之前：升级会按真实表授权动态补 EXECUTE，
        # 若先升级再授权，probe 就拿不到 EXECUTE，测到的是 42501 而非 P0001。
        for sch, tbl in ((f.split(".")[0], f.split(".")[1]) for f in mig.GUARD_ORDER):
            await conn.execute(f'GRANT USAGE ON SCHEMA "{sch}" TO {PROBE_ROLE}')
            # UPDATE 的 WHERE 子句需要 SELECT 权限，只授 UPDATE 会得到 42501
            await conn.execute(f'GRANT SELECT, UPDATE ON "{sch}"."{tbl}" TO {PROBE_ROLE}')
        pre = {}
        for cid, sql in (
            ("AV", f"UPDATE {ASSETS} SET project_id='proj-base' WHERE entity_id='av-base'"),
            ("GD", f"UPDATE {GATES} SET project_id='proj-base' WHERE entity_id='gd-base'"),
            ("QR", f"UPDATE {QCR} SET project_id='proj-base' WHERE qc_report_id='qr-base'"),
            ("AVC", f"UPDATE {ASSETS} SET sha256=repeat('c',64) WHERE entity_id='av-base'"),
        ):
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                pre[cid] = ("OK", "")
            except asyncpg.PostgresError as exc:
                pre[cid] = (exc.sqlstate, str(exc).split("\n")[0][:120])
        rec(
            "B1",
            "基线",
            "升级前：改归属列一律被拒",
            all(pre[k][0] == "P0001" for k in ("AV", "GD", "QR")),
            "; ".join(f"{k}={pre[k][0]}" for k in ("AV", "GD", "QR", "AVC")),
        )

        # ---------- 升级 ----------
        for sql in mig.upgrade_sqls():
            await conn.execute(sql)
        rec("U1", "升级", f"执行 upgrade_sqls()（{len(mig.upgrade_sqls())} 条）", True, "无异常")

        # ---------- 结构级 ----------
        n_patched = 0
        for fqn in mig.GUARD_ORDER:
            schema, table = fqn.split(".")
            body = await conn.fetchval(
                "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=$1 AND p.proname=$2",
                schema,
                table + "_guard",
            )
            if body and "ownership_repair_allows" in body:
                n_patched += 1
        rec("S1", "结构", "13 个守卫均已注入接入点", n_patched == len(mig.GUARD_ORDER), f"{n_patched}/13")

        fnrow = await conn.fetch(
            "SELECT p.proname, p.prosecdef, "
            "coalesce(array_to_string(p.proconfig, ','), '') AS cfg "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='project_planning' "
            "AND p.proname IN ('ownership_repair_allows','ownership_repair_map_match')"
        )
        fmap = {r["proname"]: r for r in fnrow}
        allows_row = fmap.get("ownership_repair_allows")
        match_row = fmap.get("ownership_repair_map_match")
        # 关键：判定函数必须是 INVOKER（secdef=False），否则 current_user 变成 owner，
        # pg_has_role 恒真、角色条件失效（F8 已实测到该缺陷）。
        s2_ok = (
            allows_row is not None
            and allows_row["prosecdef"] is False
            and match_row is not None
            and match_row["prosecdef"] is True
            and "search_path" in match_row["cfg"]
        )
        rec(
            "S2",
            "结构",
            "判定函数为 INVOKER、读映射表函数为 DEFINER 且固定 search_path",
            s2_ok,
            f"allows.secdef={allows_row['prosecdef'] if allows_row else None} "
            f"match.secdef={match_row['prosecdef'] if match_row else None} "
            f"match.cfg={match_row['cfg'] if match_row else None}",
        )

        cons = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid=$1::regclass AND contype IN ('u','c')",
            MAP,
        )
        names = {r["conname"] for r in cons}
        rec(
            "S3",
            "结构",
            "映射表唯一键与 reason 非空校验存在",
            {"uq_ownership_repair_map", "ck_ownership_repair_map_reason_len"} <= names,
            f"{sorted(names)}",
        )

        idx = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='project_planning' "
            "AND indexname='ix_ownership_repair_map_pending'"
        )
        rec("S4", "结构", "待处理部分索引存在", idx == 1, f"count={idx}")

        pub_a = await conn.fetchval(
            "SELECT has_function_privilege('public', $1::regprocedure, 'EXECUTE')",
            ALLOWS + "(regclass,jsonb,jsonb,text)",
        )
        pub_m = await conn.fetchval(
            "SELECT has_function_privilege('public', $1::regprocedure, 'EXECUTE')",
            "project_planning.ownership_repair_map_match(text,text,text)",
        )
        rec(
            "S5",
            "结构",
            "PUBLIC 对两个函数均无 EXECUTE（附录 E #15）",
            pub_a is False and pub_m is False,
            f"allows={pub_a} match={pub_m}",
        )

        rols = await conn.fetchrow("SELECT rolcanlogin FROM pg_roles WHERE rolname='afs_data_repair'")
        rec("S6", "结构", "专用角色存在且 NOLOGIN", bool(rols) and rols["rolcanlogin"] is False,
            f"canlogin={rols['rolcanlogin'] if rols else None}")

        # ---------- 判定函数级 ----------
        old_json = (
            f'{{"entity_id":"av-1","project_id":"proj-1","organization_id":"{ORG}",'
            f'"tenant_id":"{TENANT}","status":"GENERATED"}}'
        )
        new_json = old_json.replace('"project_id":"proj-1"', '"project_id":"proj-2"')

        def call(o: str = old_json, n: str = new_json, key: str = "av-1") -> str:
            return f"SELECT {ALLOWS}('{ASSETS}'::regclass, '{o}'::jsonb, '{n}'::jsonb, '{key}')"

        await conn.execute(map_insert(ASSETS, "av-1", "TCK-OK"))
        await conn.execute(map_insert(ASSETS, "av-done", "TCK-OK"))
        await conn.execute(f"UPDATE {MAP} SET applied_at=now() WHERE row_key='av-done'")

        async def sel(sql: str, setup: list[str] | None = None) -> object:
            async with conn.transaction():
                for s in setup or []:
                    await conn.execute(s)
                return await conn.fetchval(sql)

        async def sel_safe(sql: str, setup: list[str] | None = None) -> object:
            try:
                return await sel(sql, setup)
            except asyncpg.PostgresError as exc:
                return f"ERR:{exc.sqlstate}"

        f1 = await sel_safe(call())  # 无令牌
        rec("F1", "判定", "无工单令牌 → false", f1 is False, f"got={f1}")

        f2 = await sel_safe(call(key="av-none"))  # 有令牌无映射
        rec("F2", "判定", "无令牌时一切为 false（映射无关）", f2 is False, f"got={f2}")

        f3 = await sel_safe(
            call(), ["SELECT set_config('app.ownership_repair_ticket','TCK-OTHER',true)"]
        )
        rec("F3", "判定", "工单不匹配 → false", f3 is False, f"got={f3}")

        f4 = await sel_safe(
            call(), ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"]
        )
        rec("F4", "判定", "四条件满足 → true", f4 is True, f"got={f4}")

        bad_new = old_json.replace('"status":"GENERATED"', '"status":"DECIDED"')
        f5 = await sel_safe(
            call(n=bad_new), ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"]
        )
        rec("F5", "判定", "变更含非归属列 → false（内容列不受例外保护）", f5 is False, f"got={f5}")

        f6 = await sel_safe(
            call(n=old_json), ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"]
        )
        rec("F6", "判定", "无任何变更 → false", f6 is False, f"got={f6}")

        f7 = await sel_safe(
            call(key="av-done"), ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"]
        )
        rec("F7", "判定", "映射行已盖章 → false", f7 is False, f"got={f7}")

        f8 = await sel_safe(
            call(),
            [
                f"SET LOCAL ROLE {PROBE_ROLE}",
                "SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)",
            ],
        )
        f8_ok = f8 is False or (isinstance(f8, str) and f8.startswith("ERR:42501"))
        rec(
            "F8",
            "判定",
            "非 afs_data_repair 成员 → 不放行（false 或权限拒绝）",
            f8_ok,
            f"got={f8}",
        )

        # 13 表名匹配：用各表自己的行标识值构造，验证 table_name 匹配对全表成立
        tbl_ok = 0
        tbl_detail = []
        for fqn in mig.GUARD_ORDER:
            key = "qr-any" if fqn.endswith("qc_reports") else "x-any"
            await conn.execute(map_insert(fqn, key, "TCK-TBL"))
            got = await sel_safe(
                f"SELECT {ALLOWS}('{fqn}'::regclass, '{old_json}'::jsonb, '{new_json}'::jsonb, '{key}')",
                ["SELECT set_config('app.ownership_repair_ticket','TCK-TBL',true)"],
            )
            if got is True:
                tbl_ok += 1
            else:
                tbl_detail.append(f"{fqn}={got}")
        rec(
            "F9",
            "判定",
            "13 张表的 table_name 匹配全部成立",
            tbl_ok == len(mig.GUARD_ORDER),
            f"{tbl_ok}/13 {tbl_detail}",
        )

        # ---------- 端到端 ----------
        await conn.execute(asset_insert("av-e2e", tone="d"))
        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {ASSETS} SET project_id='proj-2' WHERE entity_id='av-1'",
            expect_changed=1,
        )
        val = await conn.fetchval(f"SELECT project_id FROM {ASSETS} WHERE entity_id='av-1'")
        rec("E1", "端到端", "类别 A（asset_versions）四条件满足 → 改成功且值真的变了",
            ok and val == "proj-2", f"{d} now={val}")

        ok, d = await probe(
            conn,
            [],
            f"UPDATE {ASSETS} SET project_id='proj-9' WHERE entity_id='av-e2e'",
            expect_state="P0001",
        )
        rec("E2", "端到端", "无令牌改另一行 → P0001（不是权限错误）", ok, d)

        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {ASSETS} SET sha256=repeat('e',64) WHERE entity_id='av-1'",
            expect_state="P0001",
        )
        rec("E3", "端到端", "有令牌但改内容列 → P0001", ok, d)

        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {ASSETS} SET project_id='proj-3', sha256=repeat('f',64) WHERE entity_id='av-1'",
            expect_state="P0001",
        )
        rec("E4", "端到端", "归属列+内容列同时改 → P0001（绕过尝试被拒）", ok, d)

        has_priv = await conn.fetchval(
            "SELECT has_table_privilege($1, $2, 'UPDATE') AND has_table_privilege($1, $2, 'SELECT')",
            PROBE_ROLE,
            GATES,
        )
        has_usage = await conn.fetchval(
            "SELECT has_schema_privilege($1, $2, 'USAGE')", PROBE_ROLE, "qc_governance"
        )
        rec(
            "E5a",
            "端到端",
            "探针角色的表权限已就位（否则 E5 测到的是权限拒绝）",
            has_priv is True and has_usage is True,
            f"update={has_priv} usage={has_usage}",
        )
        # 先给 gd-base 建映射：这样 probe 满足「令牌 + 映射 + 只改归属列」，
        # 唯一不满足的就是「角色」。若仍被拒，即可断定是角色条件在起作用。
        await conn.execute(map_insert(GATES, "gd-base", "TCK-OK"))
        ok, d = await probe(
            conn,
            [
                f"SET LOCAL ROLE {PROBE_ROLE}",
                "SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)",
            ],
            f"UPDATE {GATES} SET project_id='proj-2' WHERE entity_id='gd-base'",
            expect_state="P0001",
        )
        rec(
            "E5",
            "端到端",
            "非成员：其余三条件全满足仍被拒 → P0001（拦在守卫而非权限）",
            ok,
            d,
        )

        await conn.execute(gate_insert("gd-e2e"))
        await conn.execute(map_insert(GATES, "gd-e2e", "TCK-OK"))
        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {GATES} SET project_id='proj-2' WHERE entity_id='gd-e2e'",
            expect_changed=1,
        )
        gval = await conn.fetchval(f"SELECT project_id FROM {GATES} WHERE entity_id='gd-e2e'")
        rec("E6", "端到端", "类别 B（gate_decisions 无条件 RAISE）四条件满足 → 改成功",
            ok and gval == "proj-2", f"{d} now={gval}")

        await conn.execute(map_insert(QCR, "qr-base", "TCK-OK"))
        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {QCR} SET project_id='proj-2' WHERE qc_report_id='qr-base'",
            expect_changed=1,
        )
        qval = await conn.fetchval(f"SELECT project_id FROM {QCR} WHERE qc_report_id='qr-base'")
        rec("E7", "端到端", "键列为 qc_report_id 的表也能正确匹配 → 改成功",
            ok and qval == "proj-2", f"{d} now={qval}")

        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"DELETE FROM {ASSETS} WHERE entity_id='av-e2e'",
            expect_state="P0001",
        )
        rec("E8", "端到端", "DELETE 不受例外保护 → 仍 P0001", ok, d)

        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {ASSETS} SET project_id='proj-8' WHERE entity_id='av-e2e'",
            expect_state="P0001",
        )
        rec(
            "E9",
            "端到端",
            "同令牌同表但该行无映射 → P0001（例外按行，不按表）",
            ok,
            d,
        )
        # E5 的事务已回滚，gd-base 的映射 still applied_at IS NULL。
        # 换成真正的成员（postgres）执行，四条件齐备 → 应当成功。
        ok, d = await probe(
            conn,
            ["SELECT set_config('app.ownership_repair_ticket','TCK-OK',true)"],
            f"UPDATE {GATES} SET project_id='proj-2' WHERE entity_id='gd-base'",
            expect_changed=1,
        )
        rec(
            "E10",
            "端到端",
            "同一映射行：成员执行成功、非成员执行被拒（角色是唯一变量）",
            ok,
            d,
        )

        # ---------- 降级 ----------
        for sql in mig.downgrade_sqls():
            await conn.execute(sql)
        rec("D0", "降级", f"执行 downgrade_sqls()（{len(mig.downgrade_sqls())} 条）", True, "无异常")

        diff = []
        for schema, name in guards:
            now = await fn_hash(conn, schema, name)
            if now != baseline[f"{schema}.{name}"]:
                diff.append(name)
        rec("D1", "降级", "13 个守卫定义逐字恢复为升级前基线", not diff, f"不一致={diff}")

        gone_a = await conn.fetchval(
            "SELECT to_regprocedure($1) IS NULL", ALLOWS + "(regclass,jsonb,jsonb,text)"
        )
        gone_m = await conn.fetchval(
            "SELECT to_regprocedure($1) IS NULL",
            "project_planning.ownership_repair_map_match(text,text,text)",
        )
        rec("D2", "降级", "两个函数均已删除", gone_a is True and gone_m is True,
            f"allows_null={gone_a} match_null={gone_m}")

        gone_tb = await conn.fetchval("SELECT to_regclass($1) IS NULL", MAP)
        rec("D3", "降级", "映射表已删除", gone_tb is True, f"is_null={gone_tb}")

        post = {}
        for cid, sql in (
            ("AV", f"UPDATE {ASSETS} SET project_id='proj-post' WHERE entity_id='av-1'"),
            ("GD", f"UPDATE {GATES} SET project_id='proj-post' WHERE entity_id='gd-e2e'"),
            ("QR", f"UPDATE {QCR} SET project_id='proj-post' WHERE qc_report_id='qr-base'"),
            ("AVC", f"UPDATE {ASSETS} SET sha256=repeat('c',64) WHERE entity_id='av-base'"),
        ):
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                post[cid] = ("OK", "")
            except asyncpg.PostgresError as exc:
                post[cid] = (exc.sqlstate, str(exc).split("\n")[0][:120])
        same = all(pre[k] == post[k] for k in pre)
        rec(
            "Q1",
            "语义等价",
            "降级后 4 类操作的状态码与消息与升级前逐字一致",
            same,
            f"pre={ {k: v[0] for k, v in pre.items()} } post={ {k: v[0] for k, v in post.items()} }",
        )

        # ---------- 幂等 ----------
        for sql in mig.upgrade_sqls():
            await conn.execute(sql)
        try:
            for sql in mig.upgrade_sqls()[:1]:
                await conn.execute(sql)
            rec("I1", "幂等", "重复升级被前置断言拒绝", False, "未报错")
        except asyncpg.PostgresError as exc:
            rec("I1", "幂等", "重复升级被前置断言拒绝", exc.sqlstate == "P0001",
                f"{exc.sqlstate}: {str(exc).splitlines()[0][:80]}")

        for sql in mig.downgrade_sqls():
            await conn.execute(sql)
        rec("I2", "幂等", "二次降级可执行（无残留冲突）", True, "无异常")

    finally:
        await conn.close()
        admin = await asyncpg.connect(
            host=args.host, port=args.port, user=args.user, password=args.password, database="postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{args.lab}" WITH (FORCE)')
            await admin.execute(f"DROP ROLE IF EXISTS {PROBE_ROLE}")
            # 同上：角色已正式投入使用，不再由测试负责回收
            await lab.drop_role(admin, "afs_data_repair")
        finally:
            await admin.close()

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    print(f"\n受控例外验证：{passed}/{total} 通过")

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent
    out_md = out_dir / "verify-exception.md"
    out_json = out_dir / "verify-exception.json"
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "# 受控例外迁移验证结果（b0c9d8e7f6a5）",
        "",
        f"- 执行时间：{ts}",
        f"- 目标：`{args.host}:{args.port}` 克隆库 `{args.lab}`（模板 `{args.template}`），用完即删",
        f"- 结果：**{passed}/{total}**",
        "",
        "| # | 阶段 | 用例 | 结果 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['phase']} | {r['desc']} | {'PASS' if r['ok'] else '**FAIL**'} | {r['detail']} |"
        )
    lines += [
        "",
        "## 口径提醒",
        "",
        "- 本报告证明的命题是「**从激活前基线出发，b0c9 被正确应用、且可安全降级**」，"
        "不是「激活后的目标库也验过了」。B-1 在克隆库内先清行业务行再逆序回退五层迁移，"
        "因此每条用例都跑在真正的激活前结构上。",
        "- 激活后的 RLS 层行为（隔离项目可见性、静默 `UPDATE 0`、审计只读、盖章后失效）"
        "由 `rls_acceptance.py` 负责，**两个脚本互不替代**。",
        "- `E1/E6/E7/E10` 断言「`UPDATE 1` 且回读值已变」：只断言「没报错」会被静默 0 行伪装成成功。",
        "- `E5` 断言 `P0001` 而非 `42501`：若得到 42501，说明测到的是权限而不是守卫。",
        "- 模板库 `ai_film_industrial` 已不再是干净的激活前基线（内含指向隔离项目的行），"
        "B-1 的清行量已在明细里如实记录，不假设它干净。",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_json.write_text(
        json.dumps({"at": ts, "passed": passed, "total": total, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"写入 {out_md}\n写入 {out_json}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
