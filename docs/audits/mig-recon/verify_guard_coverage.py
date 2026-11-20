"""逐表覆盖矩阵：13 张守卫表是否**每一张**都真的接通了受控例外。

为什么需要这个脚本
------------------
`verify_exception.py` 的 36/36 只对 3 张表做了端到端（asset_versions / gate_decisions /
qc_reports）。「其余 10 张没跑」不等于「其余 10 张能跑」：注入点存在（S1）只证明
函数体里出现了 `ownership_repair_allows`，不证明**它被放在正确的分支上、且返回值真的
决定走向**。这中间的差别正是本项目反复出现的「恒真检查」：
S1 全绿而某张表其实永远走不到例外通道。

矩阵口径（每张表 4~5 条，全部断言 SQLSTATE 或影响行数，不看「没报错」）
--------------------------------------------------------------------
| 用例 | 前置 | 动作 | 期望 |
| N1 | 无令牌 | 改归属列 | `P0001`（守卫仍然拦） |
| N2 | 令牌 + 映射 + 成员 | 改归属列 | `UPDATE 1` **且回读值已变** |
| N3 | 令牌 + 映射 + 成员 | 改内容列 | `P0001`（例外只管归属列） |
| N4 | 令牌 + 映射 + **非成员** | 改归属列 | `P0001`（角色是唯一变量） |
| N5 | 同上 + 目标归属不存在 | 改归属列 | `23503`（例外不是外键后门，需 a9b8） |

内容列**不手写**：从系统目录里挑「非归属、不在守卫白名单、不被 CHECK/FK/UNIQUE 引用」
的第一列，按其真实类型生成变更表达式。手写 13 个内容列名称的那一刻，脚本就变成了
一份会静默漂移的手抄清单。

库隔离：全程克隆库，结束即删；模板库、两分支 head、15432、ai_film_verify、生产均不触碰。
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import asyncpg

import guard_fixtures as fx
import lab

MAP = "project_planning.ownership_repair_map"
PROBE_ROLE = "t3x_probe"
TICKET = "TCK-COV"
TICKET_FK = "TCK-COV-FK"

REC = lab.Recorder()
rec = REC.rec

# 跨表真实引用：模板里既存的父引用外键要求指向真实父行，不能填「长得像 ID」的假值。
# 只登记**确实有外键**的列；没有外键的引用列保持夹具默认值。
REF_OVERRIDES: dict[str, dict[str, str]] = {
    # qc_reports_asset_version_id_fkey → asset_versions(entity_id)
    "asset_service.qc_reports": {"asset_version_id": "asset_versions-c1"},
}

# 内容列候选：能生成「确定不同」的变更表达式；不在这里 = 不适合当负向用例的探针
_TRANSFORM = {
    "character varying": lambda c: f"{c} || '-X'",
    "text": lambda c: f"{c} || '-X'",
    "integer": lambda c: f"{c} + 1",
    "bigint": lambda c: f"{c} + 1",
    "smallint": lambda c: f"{c} + 1",
    "boolean": lambda c: f"NOT {c}",
    "jsonb": lambda c: "'{\"cov\":1}'::jsonb",
}


async def guard_whitelist(conn: asyncpg.Connection, fqn: str) -> set[str]:
    """从守卫函数体重建「可变列白名单」。

    守卫的写法是 `IF col NOT IN ('status','entity_version',...)`；不读它就无法
    可靠地区分「内容列」与「允许变的列」，只能靠猜。
    """
    schema, table = fqn.split(".")
    body = await conn.fetchval(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = $1 AND p.proname = $2",
        schema,
        table + "_guard",
    ) or ""
    names: list[str] = []
    for m in re.finditer(r"NOT\s+IN\s*\(([^)]*)\)", body, re.I):
        names += re.findall(r"'([^']+)'", m.group(1))
    for m in re.finditer(r"ALL\s*\(\s*ARRAY\s*\[([^\]]*)\]", body, re.I):
        names += re.findall(r"'([^']+)'", m.group(1))
    return set(names)


async def content_probe(conn: asyncpg.Connection, fqn: str) -> tuple[str, str] | None:
    """挑一列「一定是内容列」的字段，并给出变更表达式。"""
    white = await guard_whitelist(conn, fqn)
    rows = await conn.fetch(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS typ
          FROM pg_attribute a
         WHERE a.attrelid = $1::regclass
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attnotnull
           AND a.attnum NOT IN (
               SELECT unnest(conkey) FROM pg_constraint
                WHERE conrelid = $1::regclass AND contype IN ('c','f','u','p')
                  AND conkey IS NOT NULL
               UNION
               SELECT unnest(indkey) FROM pg_index
                WHERE indrelid = $1::regclass AND indisunique AND indkey IS NOT NULL
           )
         ORDER BY a.attnum
        """,
        fqn,
    )
    for r in rows:
        col, typ = r["attname"], r["typ"]
        if col in fx.OWNERSHIP_COLS or col in white:
            continue
        fn = _TRANSFORM.get(typ)
        if fn is None:
            continue
        return col, fn(col)
    return None


async def ownership_fk(conn: asyncpg.Connection, fqn: str) -> str | None:
    """该表上**真实存在**的归属外键名（按列查，不按名字猜）。

    不硬编码「哪两张表没有外键」：那是把迁移文件里的注释抄进测试脚本，
    抄错了测试就会安静地验证一个不存在的约束。
    """
    col = "project_id" if fx.has_project(fqn) else "organization_id"
    return await conn.fetchval(
        """
        SELECT c.conname
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = $1::regclass AND c.contype = 'f'
           AND a.attname = $2 AND a.attnum = ANY (c.conkey)
         LIMIT 1
        """,
        fqn,
        col,
    )


def map_insert(fqn: str, row_key: str, ticket: str, *, to_org: str | None = None) -> str:
    """映射行：old_* 取夹具同源值，new_* 取目标归属（默认 proj-2 / ORG2）。"""
    old = fx.old_values(fqn)
    new = fx.ownership_values(fqn, org=to_org) if to_org else fx.ownership_values(fqn)
    cols = list(old) + list(new) + ["reason", "ticket_ref"]
    vals = [f"'{v}'" if v is not None else "NULL" for v in list(old.values()) + list(new.values())]
    vals += ["'覆盖矩阵：逐表验证受控例外接通向量'", f"'{ticket}'"]
    return (
        f"INSERT INTO {MAP} (table_name, row_key, {', '.join(cols)}) "
        f"VALUES ('{fqn}', '{row_key}', {', '.join(vals)})"
    )


def sel(fqn: str, key_col: str, row_key: str, col: str) -> str:
    return f"SELECT {col} FROM {fqn} WHERE {key_col} = '{row_key}'"


async def run_table(
    conn: asyncpg.Connection,
    fqn: str,
    *,
    check_fk: bool,
    nonexistent_org: str,
) -> None:
    key_col = fx.KEY_COL[fqn]
    slug = fqn.split(".")[1]
    k = {n: f"{slug}-c{n}" for n in (1, 2, 3, 4)}

    # 造行：失败就如实记录并跳过本表（不假装通过，也不让后续用例在缺行上误判）
    try:
        for row_key in k.values():
            await conn.execute(fx.insert_sql(fqn, row_key, overrides=REF_OVERRIDES.get(fqn)))
    except asyncpg.PostgresError as exc:
        rec(f"{slug}.row", "夹具", f"{fqn} 造出 4 行合法数据", False,
            f"{exc.sqlstate}: {str(exc).splitlines()[0][:110]}")
        return

    # N1 无令牌 → 仍被守卫拦
    ok, d = await lab.case(
        conn, [], f"UPDATE {fqn} SET {fx.ownership_set(fqn)} WHERE {key_col} = '{k[1]}'",
        expect_state="P0001",
    )
    rec(f"{slug}.N1", "无令牌", "改归属列 → P0001（守卫仍在）", ok, d)

    # N2 四条件齐备 → 成功且值真的变了
    await conn.execute(map_insert(fqn, k[2], TICKET))
    ok, d = await lab.case(
        conn,
        [f"SELECT set_config('app.ownership_repair_ticket','{TICKET}',true)"],
        f"UPDATE {fqn} SET {fx.ownership_set(fqn)} WHERE {key_col} = '{k[2]}'",
        expect_changed=1,
    )
    col = "project_id" if fx.has_project(fqn) else "organization_id"
    want = fx.PROJECT_NEW if fx.has_project(fqn) else fx.ORG2
    got = await conn.fetchval(sel(fqn, key_col, k[2], col))
    # organization_id 是 uuid 列，asyncpg 返回 UUID 对象；与字符串直接比较会永远不等，
    # 把「其实成功了」判成失败。统一按字符串比较，口径与 SQL 侧一致。
    rec(f"{slug}.N2", "例外放行", f"四条件齐备 → UPDATE 1 且 {col} 回读为新值",
        ok and str(got) == want, f"{d} now={got}")

    # N3 有令牌但改内容列 → 仍被拒
    probe = await content_probe(conn, fqn)
    if probe is None:
        rec(f"{slug}.N3", "内容列", "存在可用于负向用例的内容列", False, "目录里找不到合适列")
    else:
        cname, expr = probe
        await conn.execute(map_insert(fqn, k[3], TICKET))
        ok, d = await lab.case(
            conn,
            [f"SELECT set_config('app.ownership_repair_ticket','{TICKET}',true)"],
            f"UPDATE {fqn} SET {cname} = {expr} WHERE {key_col} = '{k[3]}'",
            expect_state="P0001",
        )
        rec(f"{slug}.N3", "内容列", f"有令牌但改内容列 {cname} → P0001", ok, d)

    # N4 非成员：其余三条件全满足仍被拒
    await conn.execute(map_insert(fqn, k[4], TICKET))
    ok, d = await lab.case(
        conn,
        [f"SET LOCAL ROLE {PROBE_ROLE}",
         f"SELECT set_config('app.ownership_repair_ticket','{TICKET}',true)"],
        f"UPDATE {fqn} SET {fx.ownership_set(fqn)} WHERE {key_col} = '{k[4]}'",
        expect_state="P0001",
    )
    rec(f"{slug}.N4", "非成员", "非成员即使令牌+映射齐备 → P0001（不是 42501）", ok, d)

    # N5 例外不是外键后门（仅当该表真有归属外键）
    if check_fk:
        col = "project_id" if fx.has_project(fqn) else "organization_id"
        fk = await ownership_fk(conn, fqn)
        target = fx.nonexistent_target(fqn, nonexistent_org)
        await conn.execute(map_insert(fqn, k[2], TICKET_FK, to_org=nonexistent_org))
        action = f"UPDATE {fqn} SET {col} = '{target}' WHERE {key_col} = '{k[2]}'"
        if fk is None:
            # 没有外键时，这条更新**应当成功**——不是「跳过」，而是把缺口跑出来。
            # 只写一句「该表无外键」是断言，不是证据。
            ok, d = await lab.case(
                conn,
                [f"SELECT set_config('app.ownership_repair_ticket','{TICKET_FK}',true)"],
                action,
                expect_changed=1,
            )
            if ok:
                REC.note(
                    f"{slug}.N5", "外键", f"{col} 上是否存在归属外键",
                    f"无外键 → N5 不适用；实测该更新到不存在的目标**成功**（{d}）",
                )
                REC.finding(
                    f"{slug}.G1",
                    f"{fqn} 无归属外键",
                    f"受控例外可把 {col} 指向**不存在的项目/组织**且数据库不拦（I-1 未保护）；"
                    "唯一约束是映射表里的 new_* 记录，属流程约束而非数据库约束",
                )
            else:
                rec(f"{slug}.N5", "外键",
                    "无归属外键时该更新应当成功（缺口可复现）", False, d)
        else:
            ok, d = await lab.case(
                conn,
                [f"SELECT set_config('app.ownership_repair_ticket','{TICKET_FK}',true)"],
                action,
                expect_state="23503",
            )
            rec(f"{slug}.N5", "外键",
                f"例外放行后仍受 {fk} 约束 → 23503（非 0 行静默）", ok, d)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=lab.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    ap.add_argument("--user", default=lab.DEFAULT_USER)
    ap.add_argument("--password", default=lab.DEFAULT_PASSWORD)
    ap.add_argument("--template", default=lab.DEFAULT_TEMPLATE)
    ap.add_argument("--lab", default="t3cov_lab")
    ap.add_argument("--guard-migration", required=True)
    ap.add_argument("--fk-migration", default=None,
                    help="给出 a9b8 路径则先建归属外键，N5 才有意义")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument(
        "--versions-dir",
        default=None,
        help="迁移目录，默认取 --guard-migration 所在目录；用于把克隆库回退到激活前基线",
    )
    ap.add_argument(
        "--skip-rewind",
        action="store_true",
        help="跳过基线回退。仅在库本来就在激活前基线时才可用——"
        "模板库已升到 e3f4 后若加此开关，U0/U1 会撞上前置断言",
    )
    args = ap.parse_args()

    guard_mig = lab.load_migration(args.guard_migration, "mig_guard")
    fk_mig = lab.load_migration(args.fk_migration, "mig_fk") if args.fk_migration else None

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, args.lab, args.template)
        await lab.reset_role(admin, PROBE_ROLE)
        await lab.drop_role(admin, "afs_data_repair")
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, args.lab)
    try:
        # 模板库在归属五层迁移激活后停在 e3f4a5b6c7d8，且已写入指向隔离项目的行。
        # 本矩阵证明的是「从激活前基线出发，外键与例外都被正确应用」，因此先清行、
        # 回退、断言——不假设模板还停在原处（那个假设失效时曾被 23503 掩盖成「夹具问题」）。
        if not args.skip_rewind:
            vd = Path(args.versions_dir) if args.versions_dir else Path(args.guard_migration).parent
            purge, order, checks = await lab.prepare_pre_activation_baseline(conn, vd)
            ok = all(v.endswith("=0") for v in checks)
            rec("B0", "前置", f"回退归属五层迁移至激活前基线（{len(order)} 层）", ok,
                f"{purge}; " + "; ".join(checks))
            if not ok:
                raise RuntimeError(f"基线回退未生效：{checks}")

        # 归属外键的被引用行：没有它，N2 会以 23503 失败并被误读成「守卫拦住了」
        for pid in (fx.PROJECT_OLD, fx.PROJECT_NEW):
            await conn.execute(
                f"INSERT INTO project_planning.projects (entity_id, entity_type, schema_version, "
                f"entity_version, status, owner_module, created_at, created_by, updated_at, "
                f"project_name, project_tier, tenant_id, organization_id) VALUES "
                f"('{pid}', 'Project', 2, 1, 'ACTIVE', 'project_planning', now(), 'migx', now(), "
                f"'{pid}', 'STANDARD', '{fx.TENANT}', '{fx.ORG}')"
            )

        if fk_mig is not None:
            n = await lab.apply_steps(conn, fk_mig.upgrade_steps())
            rec("U0", "前置", f"执行归属外键迁移（{n} 步）", True, "projects 已 seed，O0 通过")
            # production_routes 的归属是两列（organization_id, tenant_id），
            # 需要第二个组织存在，否则 N2 的「转移到新组织」会撞外键。
            await conn.execute(
                "INSERT INTO project_planning.organizations "
                "(organization_id, tenant_id, name, created_at, created_by, updated_at) "
                f"VALUES ('{fx.ORG2}', '{fx.TENANT}', 'coverage-org2', now(), 'migx', now()) "
                "ON CONFLICT (organization_id) DO NOTHING"
            )
            fks = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE contype='f' AND conname LIKE 'fk\\_%\\_project\\_tenant\\_org'"
            )
            rec("U0b", "前置", "归属外键已建（NOT VALID 亦对新写入生效）",
                (fks or 0) >= 12, f"三列外键={fks}")

        # 授权必须在受控例外迁移**之前**：迁移按真实表权限动态补 EXECUTE，
        # 反序会让探针只拿到 42501，N4 就从「守卫拒绝」漂移成「权限拒绝」。
        for fqn in fx.FIXTURES:
            schema, table = fqn.split(".")
            await conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {PROBE_ROLE}')
            await conn.execute(f'GRANT SELECT, UPDATE ON {fqn} TO {PROBE_ROLE}')

        n = await lab.apply_steps(conn, guard_mig.upgrade_sqls())
        rec("U1", "前置", f"执行受控例外迁移（{n} 条）", True, "无异常")

        # 依赖前置行：shot_route_selections / generation_tasks 指向一条真实 route
        await conn.execute(fx.insert_sql("generation_execution.production_routes", fx.ROUTE_ID))

        # 不存在的归属目标：用于证明「例外放行 ≠ 绕过外键」
        missing_org = "00000000-0000-0000-0000-00000000dead"
        for fqn in fx.FIXTURES:
            await run_table(conn, fqn, check_fk=fk_mig is not None, nonexistent_org=missing_org)
    finally:
        await conn.close()
        admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await lab.drop_lab(admin, args.lab)
            await lab.drop_role(admin, PROBE_ROLE)
            await lab.drop_role(admin, "afs_data_repair")
        finally:
            await admin.close()

    print(f"\n逐表覆盖矩阵：{REC.passed}/{REC.total} 通过（N/A {REC.skipped}，待裁决缺口 {len(REC.open_findings)}）")
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent
    ts = REC.write_report(
        out_dir / "verify-coverage.md",
        out_dir / "verify-coverage.json",
        title="逐表覆盖矩阵（受控例外，13 张守卫表）",
        meta=[
            f"目标：`{args.host}:{args.port}` 克隆库 `{args.lab}`（模板 `{args.template}`），用完即删",
            f"归属外键：{'已建（' + Path(args.fk_migration).name + '）' if args.fk_migration else '**未建**，N5 未执行'}",
        ],
        footer=[
            "## 口径提醒",
            "",
            "- 本矩阵证明的命题是「**从激活前基线出发**，13 张表各自都真的接通了受控例外」。"
            "B0 在克隆库内先清行业务行再逆序回退五层迁移，故 U0/U1 重放的是真实迁移体，"
            "而不是在一个已激活的库上做假升级。",
            "- 激活后的 RLS 层行为由 `rls_acceptance.py` 负责，本矩阵不覆盖 RLS 分支。",
            "- N2 断言 `UPDATE 1` **且回读值已变**：`UPDATE 0` 也算失败（RLS 会静默过滤）。",
            "- N4 断言 `P0001` 而非 `42501`：若得到 42501，说明测到的是权限而不是守卫。",
            "- 内容列由系统目录推导，不由脚本手写；表结构变化会改变探针列，但不会让用例失声。",
            "- N/A 不是通过：它表示该用例的前置条件按设计不存在，因此**不计入通过率**，"
            "其背后的缺口单列在「待裁决缺口」。",
        ],
    )
    print(f"[{ts}] 写入 {out_dir / 'verify-coverage.md'}")
    return 0 if REC.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
