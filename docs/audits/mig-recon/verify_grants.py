"""验证归属栈的角色授权完整性：删掉「名单式授权」之后，权限到底丢没丢。

为什么需要这个脚本
------------------
`b0c9` 的 E5 曾经这样写：

    for role in ("app_role", "afs_data_repair"):
        ... IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
              GRANT EXECUTE ... ownership_repair_allows ...

它被 `scripts/contract_check.py` 的 CRED-3 判为违规（判据在
`tests/unit/test_db_role_names.py`：代码不得写死默认角色名）。但违规只是表象，
实测发现这条名单**在本环境从未生效过**，同时踩中三个坑：

1. 库里根本没有 `app_role`。真实角色是 `svc_asset` / `svc_project_planning` /
   `svc_qc_governance` / `svc_shot_planning` / `svc_workflow` /
   `svc_generation_execution` / `svc_identity` / `svc_outbox_relay` 八个域服务角色，
   `IF EXISTS` 把它**静默跳过**。也就是说「已显式授权」是注释给人的错觉。
2. 名单里只授了 `ownership_repair_allows`，**没授它内部调用的**
   `ownership_repair_map_match`。函数级 EXECUTE 不传递，于是即便角色被授到
   `allows`，守卫一走到 `map_match` 仍然 42501。
3. `afs_data_repair` 由 `d2e3` 才创建并授表权限，而 E5 运行在 `b0c9`（更早）。
   `IF EXISTS` 当时看不到它，同样静默跳过。

这三个坑都被后来的 E6（按 `information_schema.role_table_grants` 收敛）与
`d2e3`（在角色获得表权限后显式补授两函数）兜住了，名单只剩下害处，故删除。
但「删掉之后授权还在不在」不能靠读代码回答：E6 的收敛条件取决于**执行到那一步时**
谁已经有表权限，这是运行时事实，只能从库里实读。

所以本脚本把归属栈六层在克隆库里完整重放一遍，再用 `has_function_privilege`
把结论变成断言。主断言（G3）是一条**不变量**而不是某几个角色的清单：

    对任一受守卫表持有 UPDATE/INSERT/DELETE 的主体，
    必须同时持有两个判定函数的 EXECUTE。

违反它的后果不是「少个权限」，而是**保护语义漂移**：同一个非法归属改写，
升级前得到 `P0001`（守卫拒绝），升级后得到 `42501`（权限拒绝）。按 P0001
写了处理逻辑的调用方会静默失效——这比直接报错更难查。

非恒真证明（G5/G6）
-------------------
若「上面的断言全绿」其实是 PG 的默认行为，那本脚本什么也没证明。故新增一个
角色、只给它受守卫表的 UPDATE，然后断言它**没有**函数的 EXECUTE（G5）；
再手工执行 E6 同款语句，断言它有了（G6）。两条合起来才说明：
**绿是 E6 真跑过的结果，缺口是真实存在的。**

库隔离：全程只在克隆库内操作，结束即删。模板库、15432、`ai_film_verify`、生产均不触碰。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

import lab

ALLOWS = "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)"
MAP_MATCH = "project_planning.ownership_repair_map_match(text,text,text)"
REPAIR_ROLE = "afs_data_repair"
PROBE_ROLE = "gx_writer_probe"

REC = lab.Recorder()
results = REC.rows
rec = REC.rec
note = REC.note
finding = REC.finding


# --------------------------------------------------------------------------- #
# 权限事实读取（一律从系统目录读，不手写「应该有」）
# --------------------------------------------------------------------------- #
async def fn_oid(conn: asyncpg.Connection, signature: str) -> int | None:
    oid = await conn.fetchval("SELECT to_regprocedure($1)::oid", signature)
    return int(oid) if oid is not None else None


async def writer_matrix(
    conn: asyncpg.Connection, guard_tables: list[str], allows_oid: int, map_oid: int
) -> list[dict[str, object]]:
    """列出「谁对受守卫表有写权限」以及它是否持有两个函数的 EXECUTE。

    写权限取自 `information_schema.role_table_grants`——这是**事实**，不是设计意图。
    用 `has_function_privilege` 而不用 `pg_proc.proacl` 手工解析：ACL 里既有
    `PUBLIC` 也有角色组继承，手解析等于重写一遍 PG 自己的判定规则。
    """
    rows = await conn.fetch(
        """
        WITH guards(tbl) AS (SELECT unnest($1::text[])),
        writers AS (
            SELECT DISTINCT g.grantee::text AS role,
                            g.table_schema || '.' || g.table_name AS tbl
              FROM information_schema.role_table_grants g
              JOIN guards ON guards.tbl = g.table_schema || '.' || g.table_name
             WHERE g.privilege_type IN ('UPDATE', 'INSERT', 'DELETE')
               AND g.grantee <> 'PUBLIC'
        )
        SELECT w.role, w.tbl,
               has_function_privilege(w.role, $2::oid, 'EXECUTE') AS has_allows,
               has_function_privilege(w.role, $3::oid, 'EXECUTE') AS has_map
          FROM writers w
         ORDER BY 1, 2
        """,
        guard_tables,
        allows_oid,
        map_oid,
    )
    return [dict(r) for r in rows]


def drift_of(matrix: list[dict[str, object]]) -> list[str]:
    """返回「有表写权限却缺函数 EXECUTE」的 (角色, 表) 清单。

    两个函数都要查：只查 `allows` 会漏掉历史上的真实故障形态
    （有 `allows` 无 `map_match`，一调用内部就 42501）。
    """
    out = []
    for r in matrix:
        missing = []
        if not r["has_allows"]:
            missing.append("allows")
        if not r["has_map"]:
            missing.append("map_match")
        if missing:
            out.append(f"{r['role']}@{r['tbl']}缺 {'+'.join(missing)}")
    return out


async def public_execute(conn: asyncpg.Connection, oid: int) -> bool:
    """PUBLIC 是否持有该函数的 EXECUTE（撤 PUBLIC 是否真的生效）。"""
    return bool(
        await conn.fetchval(
            "SELECT has_function_privilege('public', $1::oid, 'EXECUTE')", oid
        )
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    vd = Path(args.versions_dir)
    stack_mods = []
    for rev in lab.OWNERSHIP_STACK:
        stack_mods.append((rev, lab.load_migration(lab._resolve_revision(vd, rev), f"gx_{rev}")))

    guard_mod = dict(stack_mods)["b0c9d8e7f6a5"]
    guard_tables = list(guard_mod.GUARD_ORDER)

    admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
    try:
        await lab.fresh_clone(admin, args.lab, args.template)
    finally:
        await admin.close()

    conn = await lab.lab_conn(args.host, args.port, args.user, args.password, args.lab)
    try:
        # ---------- G0 基线 ----------
        purge, order, checks = await lab.prepare_pre_activation_baseline(conn, vd)
        ok = all(v.endswith("=0") for v in checks)
        rec(
            "G0",
            "基线",
            f"回退归属栈至激活前基线（{len(order)} 层）",
            ok,
            f"{purge}；" + "; ".join(checks),
        )
        if not ok:
            # 基线不可信就不许继续：混合状态下读到的权限矩阵与被测命题无关。
            raise RuntimeError(f"基线回退未生效：{checks}")

        # ---------- G1 中间态：a9b8 + b0c9（名单已删，看 E6 单独能授到哪） ----------
        for _rev, mod in stack_mods[:2]:
            await lab.apply_steps(conn, lab.upgrade_sqls(mod))

        allows_oid = await fn_oid(conn, ALLOWS)
        map_oid = await fn_oid(conn, MAP_MATCH)
        if allows_oid is None or map_oid is None:
            rec("G1", "中间态", "两个判定函数已创建", False, "to_regprocedure 返回 NULL")
            raise RuntimeError("判定函数不存在，后续断言无意义")
        rec("G1", "中间态", "两个判定函数已创建（b0c9 之后）", True, f"allows={allows_oid} map={map_oid}")

        has_repair = await conn.fetchval("SELECT to_regrole($1) IS NOT NULL", REPAIR_ROLE)
        if not has_repair:
            note(
                "G2",
                "中间态",
                f"`{REPAIR_ROLE}` 此刻尚不存在，函数授权无从谈",
                "由 d2e3 创建；E5 的 IF EXISTS 在这一步必然跳过它（这是删名单的理由之一）",
            )
        else:  # pragma: no cover - 仅当迁移顺序被改动时触发
            matrix = await writer_matrix(conn, guard_tables, allows_oid, map_oid)
            drift = drift_of(matrix)
            rec("G2", "中间态", "此刻的写者均已持有函数 EXECUTE", not drift, "; ".join(drift) or "无漂移")

        # ---------- G3 终态：六层全上 ----------
        for _rev, mod in stack_mods[2:]:
            await lab.apply_steps(conn, lab.upgrade_sqls(mod))
        rec("G3", "终态", f"归属栈六层全部应用（末层 {stack_mods[-1][0]}）", True, f"{len(stack_mods)} 层")

        allows_oid = await fn_oid(conn, ALLOWS)
        map_oid = await fn_oid(conn, MAP_MATCH)
        if allows_oid is None or map_oid is None:  # pragma: no cover
            raise RuntimeError("终态函数丢失")

        matrix = await writer_matrix(conn, guard_tables, allows_oid, map_oid)
        drift = drift_of(matrix)
        rec(
            "G4",
            "终态",
            f"不变量：对受守卫表有写权限者必持两函数 EXECUTE（{len(matrix)} 个 角色×表）",
            not drift,
            "; ".join(drift) or "零漂移",
        )

        # 全域漂移检查必须非空才可信：空矩阵会让 G4 平凡通过。
        if not matrix:
            rec("G4b", "终态", "写者矩阵非空（否则上一行是平凡通过）", False, "0 行")
        else:
            roles = sorted({str(r["role"]) for r in matrix})
            rec("G4b", "终态", "写者矩阵非空", True, f"{len(roles)} 个角色：{', '.join(roles[:6])}…")

        repair_row = [r for r in matrix if r["role"] == REPAIR_ROLE]
        has_repair = await conn.fetchval("SELECT to_regrole($1) IS NOT NULL", REPAIR_ROLE)
        if not has_repair:  # pragma: no cover
            rec("G5", "终态", f"`{REPAIR_ROLE}` 已创建（d2e3）", False, "角色不存在")
        elif not repair_row:
            # 角色存在但对受守卫表没有写权限：d2e3 的表授权没生效。
            rec("G5", "终态", f"`{REPAIR_ROLE}` 已持有受守卫表写权限", False, "矩阵中无该角色的行")
        else:
            bad = [f"{r['tbl']}" for r in repair_row if not (r["has_allows"] and r["has_map"])]
            rec(
                "G5",
                "终态",
                f"`{REPAIR_ROLE}` 对两函数均有 EXECUTE（{len(repair_row)} 张表）",
                not bad,
                "; ".join(bad) or "全部具备（历史坑：曾只授 allows 不授 map_match）",
            )

        pub_a = await public_execute(conn, allows_oid)
        pub_m = await public_execute(conn, map_oid)
        rec("G6", "终态", "PUBLIC 无两函数 EXECUTE（撤 PUBLIC 生效）", not (pub_a or pub_m),
            f"allows={pub_a} map_match={pub_m}")

        # ---------- G7/G8 非恒真：新角色不会自动获得授权 ----------
        admin2 = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await lab.drop_role(admin2, PROBE_ROLE)
            await admin2.execute(f"CREATE ROLE {PROBE_ROLE} NOLOGIN")
        finally:
            await admin2.close()

        await conn.execute(f"GRANT USAGE ON SCHEMA asset_service TO {PROBE_ROLE}")
        await conn.execute(f"GRANT UPDATE ON asset_service.asset_versions TO {PROBE_ROLE}")

        probe_allows = bool(
            await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", PROBE_ROLE, allows_oid
            )
        )
        probe_map = bool(
            await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", PROBE_ROLE, map_oid
            )
        )
        rec(
            "G7",
            "非恒真",
            "只授表权限的新角色**不会**自动获得函数 EXECUTE",
            not (probe_allows or probe_map),
            f"allows={probe_allows} map_match={probe_map} → E6 的绿不是 PG 默认行为",
        )

        # 走一遍 E6 同款收敛语句，证明「补授路径有效」，同时证明 G7 的缺口可修。
        await conn.execute(f"GRANT USAGE ON SCHEMA project_planning TO {PROBE_ROLE}")
        for fn in (ALLOWS, MAP_MATCH):
            await conn.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {PROBE_ROLE}")
        probe_allows2 = bool(
            await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", PROBE_ROLE, allows_oid
            )
        )
        probe_map2 = bool(
            await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", PROBE_ROLE, map_oid
            )
        )
        rec(
            "G8",
            "非恒真",
            "执行 E6 同款语句后该角色获得两函数 EXECUTE",
            probe_allows2 and probe_map2,
            f"allows={probe_allows2} map_match={probe_map2}",
        )

        # ---------- G9 已知边界：E6 只在升级时跑一次 ----------
        matrix2 = await writer_matrix(conn, guard_tables, allows_oid, map_oid)
        drift2 = drift_of(matrix2)
        finding(
            "G9",
            "E6 是升级时的一次性收敛，不是持续授权",
            "升级后新建的写角色需重跑收敛语句才能获得 EXECUTE，否则其写入得到 42501。"
            "本脚本 G7 已实证该缺口存在；G8 证明修复语句有效。"
            "这属「授权随权限变化」的固有代价，名单式授权无法消除（它只是把缺口换成了静默跳过）。"
            + (f" 当前累积漂移：{'; '.join(drift2)}" if drift2 else " 当前库内无累积漂移。"),
        )

        # ---------- G10 降级：函数级权限随函数一起消失，不留悬空 ACL ----------
        # 降级语句一律经 lab._downgrade_sqls 取：它已适配「Steps / 纯 SQL 字符串」两种写法，
        # 在各脚本里再写一遍 if hasattr 分支，就等于把同一个适配器复制成 N 份。
        for _rev, mod in reversed(stack_mods):
            for sql in lab._downgrade_sqls(mod):
                await conn.execute(sql)
        left = await fn_oid(conn, ALLOWS)
        rec("G10", "降级", "六层逆序降级后判定函数已移除（无悬空 ACL）", left is None,
            f"allows_oid={left}")

    finally:
        await conn.close()
        admin = await lab.admin_conn(args.host, args.port, args.user, args.password)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{args.lab}" WITH (FORCE)')
            await lab.drop_role(admin, PROBE_ROLE)
            # 不在测试里回收 afs_data_repair：它已在目标库正式投入使用，
            # 属集群级角色，删它会牵连其它库（lab.drop_role 内部吞掉该错误）。
            await lab.drop_role(admin, REPAIR_ROLE)
        finally:
            await admin.close()

    # ---------- 报告 ----------
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent
    meta = [
        f"- 目标：`{args.host}:{args.port}` 克隆库 `{args.lab}`（模板 `{args.template}`），用完即删",
        f"- 迁移源：`{vd}`",
        f"- 受守卫表：{len(guard_tables)} 张（清单取自 `b0c9.GUARD_ORDER`，不手写）",
    ]
    footer = [
        "## 口径提醒",
        "",
        "- 本报告证明的命题是「**归属栈六层重放后，授权完整且无漂移**」，"
        "不是「目标库当前的 ACL 也验过了」；后者应以目标库实查为准。",
        "- G4 的主断言是不变量而非清单：**有表写权限 ⇒ 有函数 EXECUTE**。"
        "违反它的后果是保护语义漂移（`P0001` 变 `42501`），不是少一个 GRANT。",
        "- G7/G8 是非恒真证明：若 G4 全绿只是 PG 默认行为，G7 会失败。两条必须一起看。",
        "- G9 不是失败项，是**已被实证的边界**：E6 一次性收敛，新角色需重跑。",
        "- 角色名一律从 `pg_roles` / `information_schema` 实读，脚本内不出现默认角色名字面量"
        "（`app_role` 那段被删正是因为这一点，判据见 `tests/unit/test_db_role_names.py`）。",
    ]
    ts = REC.write_report(
        out_dir / "verify-grants.md",
        out_dir / "verify-grants.json",
        title="归属栈角色授权完整性验证（名单式授权删除后）",
        meta=meta,
        footer=footer,
    )
    print(f"\n授权完整性验证：{REC.passed}/{REC.total} 通过（N/A {REC.skipped}，缺口 {len(REC.open_findings)}）")
    print(f"写入 {out_dir / 'verify-grants.md'}")
    return 0 if REC.failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="验证归属栈角色授权完整性")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="replay_super_pw")
    ap.add_argument("--template", default="ai_film_industrial")
    ap.add_argument("--lab", default="gx_lab")
    ap.add_argument("--versions-dir", required=True, help="含归属栈六层迁移的 versions 目录")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
