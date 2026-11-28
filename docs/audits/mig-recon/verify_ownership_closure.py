"""归属闭环「覆盖完整性」闸门：证明台账之外没有别的归属表。

为什么需要这道闸门
------------------
同一类错误已经发生过两次，两次都不是「判断错了」，而是**压根没被列入判断**：

1. `asset_service.qc_reports` —— 既不在 `a9b8.PROJECT_LEVEL`，也不在
   `a9b8.DERIVED_NO_FK`。一个既不在「做了」也不在「不做，理由如下」清单里的对象，
   在建表、建 RLS、建守卫、装受控例外这四轮里全部被跳过，直到逐表覆盖矩阵
   暴露它的 N5 是 N/A 才被发现。
2. `project_planning.episodes` —— 有 `project_id`、有 `project_scope_episodes`
   （RESTRICTIVE）、被 `d2e3` 授了修数权限、被 `e3f4` 装了旁路，**唯独没有归属外键**，
   也不在台账里。例外通道为它完整地建好了，而它本该守护的那个约束从未创建。

这两次的共同点：**漏掉的条目不会自己举手**。靠人工维护的清单必然有这一类遗漏，
所以判据不能是「清单里每一项都做对了吗」，必须是
**「清单本身完整吗」**。

判据（集合相等，两侧都可机检）
------------------------------
::

    含 project_id 的表  ==  台账 A 类行  ∪  豁免（每条带可机检前提）
    含 organization_id 且无 project_id 的表  ==  台账 B/ROOT 行  ∪  豁免

台账一侧**从 `acceptance_ledger.ledger_rows()` 取，本脚本不另写一份**。否则闸门
自己就成了第二个事实源——而「一个事实一个权威源」正是它要守的规则。

为什么豁免不是免费通行证
------------------------
豁免表极容易退化成垃圾桶（「这张先不算」）。所以每条豁免必须携带一个
**可机检的前提**，前提不成立即 FAIL：

* `script_revisions`：project 归属由 `(project_id, episode_ref) → episodes` 复合外键
  强制。只要父行 `episodes` 在闭环内，本行的 project 一致性就是**传递成立**的。
  这不是「不建」，而是「用父行的约束代替自己的」——必须能被 SQL 验证。
* `organizations`：归属根。全库没有任何实体能当它的父，它是链条终点而不是漏洞。

覆盖不到的边界（如实写在报告里，不假装解决）
--------------------------------------------
本闸门只回答「有没有被纳入」，不回答「纳入的那条约束是否足够强」。
「足够强」分两半，各有专门的闸门：

* **I-1「真实」半边**（目标行存在、org/tenant 与目标一致）——
  由 `acceptance_ledger.py` 的 A/B/ROOT 行覆盖；
* **I-1「一致」半边**（本行 project 与**父行**相同）——
  由 `verify_ownership_consistency.py` 覆盖。三列外键**不**提供这一半：
  `qc_reports` 挂在 `proj-alpha` 的资产上、自己写 `proj-beta`，两个项目都真实、
  组织也一致，三列外键一路放行。该缺口已由批次四（`h8c9`）补齐，
  但「补齐」这件事由那个闸门证明，不由本脚本自称。

本闸门也因此**不**把「台账 28 行全 PASS」读成「保护完备」——它只保证
这 28 行都被真实约束接住了。

用法::

    python docs/audits/mig-recon/verify_ownership_closure.py --db ai_film_industrial --port 15444
    python docs/audits/mig-recon/verify_ownership_closure.py --selftest   # 证明它能 FAIL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import acceptance_ledger as ledger
import asyncpg
import lab

OUT_MD = Path(__file__).resolve().parent / "verify-ownership-closure.md"
OUT_JSON = Path(__file__).resolve().parent / "verify-ownership-closure.json"

#: 系统目录实读：哪些表带归属列。刻意从 `pg_attribute` 取而不从迁移常量取——
#: 迁移常量是「我以为有哪些表」，系统目录是「实际有哪些表」。两者不一致时，
#: 要以实际为准并让人看见差异。
CATALOG_SQL = """
SELECT n.nspname || '.' || c.relname AS tbl,
       bool_or(a.attname = 'project_id')        AS has_project,
       bool_or(a.attname = 'organization_id')   AS has_org
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
 WHERE c.relkind = 'r'
   AND a.attnum > 0 AND NOT a.attisdropped
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND n.nspname NOT LIKE 'pg_%'
 GROUP BY 1
HAVING bool_or(a.attname IN ('project_id', 'organization_id'))
 ORDER BY 1
"""


@dataclass(frozen=True)
class Exemption:
    """一条「在闭环之外，但有机器可验证的理由」的记录。

    `precondition` 必须是**返回 bool 的 SQL**，而不是一段说明文字。文字会腐烂，
    SQL 会在前提失效的那一天自己变红。这是豁免机制能长期成立的前提。
    """

    table: str
    kind: str  # "derived" 派生 / "root" 根
    reason: str  # 为什么可以不算
    precondition: str  # 机检：为什么现在仍然成立


#: 豁免清单。新增条目必须同时给出 `precondition`，且该条件要能区分
#: 「设计如此」与「本该建而没建」——后者必须进台账，不许进这里。
EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption(
        "project_planning.script_revisions",
        "derived",
        "project 归属由 (project_id, episode_ref) → episodes 复合外键强制；"
        "父行在闭环内，本行的 project 一致性传递成立",
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid = 'project_planning.script_revisions'::regclass
               AND contype = 'f'
               AND confrelid = 'project_planning.episodes'::regclass
               AND (SELECT array_agg(a.attname ORDER BY k.ord)
                      FROM unnest(conkey) WITH ORDINALITY k(attnum, ord)
                      JOIN pg_attribute a
                        ON a.attrelid = conrelid AND a.attnum = k.attnum)
                   = ARRAY['project_id', 'episode_ref']::name[]
               AND convalidated
        )
        """,
    ),
    Exemption(
        "project_planning.organizations",
        "root",
        "归属根：全库归属链的终点，唯一上一级是 projects 对它的外键",
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE convalidated
               AND conrelid = 'project_planning.projects'::regclass
               AND confrelid = 'project_planning.organizations'::regclass
        )
        """,
    ),
    Exemption(
        "audit.status_transitions",
        "derived",
        "审计事实表跨对象记录状态迁移，归属由 resource_type/resource_id 指向的对象承担；"
        "本表不持有 project_id，因而不属于单一项目归属链",
        """
        SELECT to_regclass('audit.status_transitions') IS NOT NULL
           AND NOT EXISTS (
                SELECT 1 FROM pg_attribute
                 WHERE attrelid = to_regclass('audit.status_transitions')
                   AND attname = 'project_id'
                   AND NOT attisdropped
           )
        """,
    ),
)


def _ledger_sets() -> tuple[set[str], set[str]]:
    """台账分两类：项目级（A）与组织级/根（B/ROOT）。

    刻意从 `ledger_rows()` 取：本脚本的价值在「比对」，不在「再声明一遍」。
    """
    rows = ledger.ledger_rows()
    project_level = {r.table for r in rows if r.kind == "A"}
    org_level = {r.table for r in rows if r.kind in ("B", "ROOT")}
    return project_level, org_level


async def _fetch_catalog(conn: asyncpg.Connection) -> dict[str, dict[str, bool]]:
    rows = await conn.fetch(CATALOG_SQL)
    return {r["tbl"]: {"project": r["has_project"], "org": r["has_org"]} for r in rows}


async def _constraint_state(conn: asyncpg.Connection, table: str, name: str) -> tuple[bool, bool]:
    """返回 (存在, 已验证)。表本身不存在时也返回 (False, False)，不抛错。"""
    row = await conn.fetchrow(
        """
        SELECT convalidated FROM pg_constraint
         WHERE conname = $1 AND conrelid = to_regclass($2)
        """,
        name,
        table,
    )
    if row is None:
        return False, False
    return True, row["convalidated"]


def _fmt(names: set[str]) -> str:
    return "、".join(f"`{n}`" for n in sorted(names)) if names else "—"


async def run(db: str, port: int, host: str) -> int:
    rec = lab.Recorder()
    project_level, org_level = _ledger_sets()
    exemptions = {e.table for e in EXEMPTIONS}

    conn = await asyncpg.connect(
        host=host, port=port, user="postgres", password=lab.DEFAULT_PASSWORD, database=db
    )
    try:
        catalog = await _fetch_catalog(conn)

        with_project = {t for t, v in catalog.items() if v["project"]}
        org_only = {t for t, v in catalog.items() if v["org"] and not v["project"]}

        # ---------- C1/C2 集合相等：漏在哪一侧都要说出来 ----------
        uncovered = with_project - project_level - exemptions
        rec.rec(
            "C1",
            "覆盖",
            f"含 project_id 的表全部被纳入（{len(with_project)} 张）",
            not uncovered,
            f"未覆盖：{_fmt(uncovered)}" if uncovered else f"台账 A {len(project_level)} + 豁免",
        )

        phantom = project_level - with_project
        rec.rec(
            "C2",
            "覆盖",
            "台账 A 类行都对应真实存在且带 project_id 的表（防虚报）",
            not phantom,
            f"台账有而库无：{_fmt(phantom)}" if phantom else f"A 类 {len(project_level)} 行均落地",
        )

        uncovered_org = org_only - org_level - exemptions
        rec.rec(
            "C3",
            "覆盖",
            f"组织级表全部被纳入（{len(org_only)} 张）",
            not uncovered_org,
            f"未覆盖：{_fmt(uncovered_org)}"
            if uncovered_org
            else f"台账 B/ROOT {len(org_level)} + 豁免",
        )

        phantom_org = org_level - catalog.keys()
        rec.rec(
            "C4",
            "覆盖",
            "台账 B/ROOT 类行都对应真实存在的表",
            not phantom_org,
            f"台账有而库无：{_fmt(phantom_org)}" if phantom_org else "均落地",
        )

        # ---------- C5 豁免的前提必须仍然成立 ----------
        bad_ex: list[str] = []
        for ex in EXEMPTIONS:
            try:
                ok = bool(await conn.fetchval(ex.precondition))
            except asyncpg.PostgresError as exc:  # 前提 SQL 自身失效也是 FAIL
                ok = False
                bad_ex.append(f"{ex.table}（前提 SQL 报错 {exc.sqlstate}）")
                continue
            if not ok:
                bad_ex.append(ex.table)
        rec.rec(
            "C5",
            "豁免",
            f"{len(EXEMPTIONS)} 条豁免的机检前提全部成立",
            not bad_ex,
            f"前提失效：{_fmt(set(bad_ex))}" if bad_ex else "derived/root 前提逐条实检通过",
        )

        # ---------- C6 豁免不能掩盖「本该建却没建」 ----------
        # 判据：豁免表若在闭环之外、却**也**带 organization_id 与 tenant_id，
        # 说明它具备挂三列外键的全部条件（列齐备），此时「派生」必须由复合外键
        # 真的兜住，否则就是拿豁免掩盖缺口。C5 已验前提，这里补一条反向检查：
        # 豁免集合与台账集合不得重叠（重叠说明同一张表被两套口径分别处理）。
        overlap = exemptions & (project_level | org_level)
        rec.rec(
            "C6",
            "豁免",
            "豁免集合与台账集合不相交",
            not overlap,
            f"重复登记：{_fmt(overlap)}" if overlap else "每张表只有一个口径",
        )

        # ---------- C7 台账每一行的约束真实存在且已验证 ----------
        missing: list[str] = []
        pending: list[str] = []
        for row in ledger.ledger_rows():
            exists, validated = await _constraint_state(conn, row.table, row.constraint)
            if not exists:
                missing.append(row.table)
            elif not validated:
                pending.append(row.table)
        rec.rec(
            "C7",
            "台账",
            f"台账 {len(ledger.ledger_rows())} 行的约束全部存在",
            not missing,
            f"缺失：{_fmt(set(missing))}" if missing else "全部命中 pg_constraint",
        )
        rec.rec(
            "C8",
            "台账",
            "台账每一行的约束均已 VALIDATE（存在 ≠ 闭环）",
            not pending,
            f"待验证：{_fmt(set(pending))}" if pending else "convalidated 全为 true",
        )
    finally:
        await conn.close()

    meta = [
        f"目标库：`{db}` @ `{host}:{port}`",
        "台账集合来源：`acceptance_ledger.ledger_rows()`（不在本脚本重写）",
        "目录集合来源：`pg_class`/`pg_attribute` 实读（不读迁移常量）",
        f"豁免：{len(EXEMPTIONS)} 条，每条带可机检 SQL 前提",
    ]
    footer = [
        "## 口径提醒",
        "",
        "- 本闸门只回答「**有没有被纳入**」，不回答「纳入的约束够不够强」。",
        "  三列外键仍只保「目标真实 + 租户组织一致」，**不保本行 project 与父行相同**。",
        "- C8 与 `acceptance_ledger.py` 有重叠但不冗余：台账判「这一行闭环了吗」，",
        "  本闸门判「台账这张清单完整吗」。前者全绿而后者红，正是 `episodes` 那一类。",
        "- 豁免的前提是 SQL 不是文字：前提失效（如父行复合外键被删）时 C5 会变红，",
        "  而不是等到下一轮人工复核。",
    ]
    rec.write_report(
        OUT_MD,
        OUT_JSON,
        title="归属闭环覆盖完整性闸门（台账之外是否还有归属表）",
        meta=meta,
        footer=footer,
    )
    OUT_JSON.write_text(
        json.dumps(
            {
                "db": db,
                "passed": rec.passed,
                "failed": rec.failed,
                "total": rec.total,
                "rows": rec.rows,
                "exemptions": [e.table for e in EXEMPTIONS],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果：{rec.passed}/{rec.total}   报告：{OUT_MD.name}")
    return 0 if rec.failed == 0 else 1


async def selftest(port: int, template: str) -> int:
    """证明闸门两侧都真的读了数据库，而不是「台账对自己说了什么」。

    为什么这条自检必须这样设计
    --------------------------
    最初写成「基线库上应当报出 ≥10 张未覆盖」，跑出来只有 1 张——因为基线库上
    台账集合仍是那 15 张常量，C1 判的是「表在不在清单里」，与约束存不存在无关。
    这个失败暴露了自检本身的前提错误：**用 A 的失败去证明 B 有效**。

    所以分两侧各证一次，每侧都构造一个「必须变红」的输入：

    * S1（目录侧）：在克隆库建一张合成的带 `project_id` 表。若 C1 仍绿，说明它读的
      是硬编码清单而不是 `pg_attribute`，那它永远抓不到 `episodes` 这一类。
    * S2（台账侧）：基线库上一条归属外键都不存在，C7 必须报出全部缺失。若 C7 仍绿，
      说明它只看台账自述，不看 `pg_constraint`。

    两侧都红，闸门才是真的在比对两个独立来源。
    """
    probe = "t3_closure_lab"
    admin = await lab.admin_conn(lab.DEFAULT_HOST, port, "postgres", lab.DEFAULT_PASSWORD)
    try:
        await lab.fresh_clone(admin, probe, template)
    finally:
        await admin.close()

    conn = await asyncpg.connect(
        host=lab.DEFAULT_HOST,
        port=port,
        user="postgres",
        password=lab.DEFAULT_PASSWORD,
        database=probe,
    )
    try:
        _purge, _order, checks = await lab.prepare_pre_activation_baseline(
            conn,
            Path(__file__).resolve().parents[3] / "data/migrations/pending/feat-ownership",  # noqa: ASYNC240 — CLI 启动期一次性路径解析，非并发热路径
        )
        baseline_ok = all(v.endswith("=0") for v in checks)
        print(f"[{'PASS' if baseline_ok else 'FAIL'}] S0  基线回退生效：{'; '.join(checks)}")

        project_level, _ = _ledger_sets()
        exemptions = {e.table for e in EXEMPTIONS}

        # ---------- S1 目录侧：合成表必须被 C1 抓出来 ----------
        await conn.execute("CREATE TABLE public.zz_synthetic_owned (project_id varchar(64))")
        try:
            catalog = await _fetch_catalog(conn)
            with_project = {t for t, v in catalog.items() if v["project"]}
            uncovered = with_project - project_level - exemptions
            got_synth = "public.zz_synthetic_owned" in uncovered
        finally:
            await conn.execute("DROP TABLE public.zz_synthetic_owned")

        print(
            f"[{'PASS' if got_synth else 'FAIL'}] S1  目录侧生效：合成的 project_id 表被报为未覆盖"
            f"（未覆盖 {len(uncovered)} 张）"
        )

        # ---------- S2 台账侧：基线库上约束全缺，C7 必须红 ----------
        rows = ledger.ledger_rows()
        missing = []
        for row in rows:
            exists, _validated = await _constraint_state(conn, row.table, row.constraint)
            if not exists:
                missing.append(row.table)
        c7_red = len(missing) == len(rows)
        print(
            f"[{'PASS' if c7_red else 'FAIL'}] S2  台账侧生效：基线库上 C7 报出全部 {len(rows)} 条约束缺失"
            f"（实缺 {len(missing)}）"
        )

        ok = baseline_ok and got_synth and c7_red
        return 0 if ok else 1
    finally:
        await conn.close()


async def main_async(args: argparse.Namespace) -> int:
    if args.selftest:
        return await selftest(args.port, args.template)
    return await run(args.db, args.port, args.host)


def main() -> int:
    ap = argparse.ArgumentParser(description="归属闭环覆盖完整性闸门")
    ap.add_argument("--db", default="ai_film_industrial")
    ap.add_argument("--host", default=lab.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=lab.DEFAULT_PORT)
    ap.add_argument("--selftest", action="store_true", help="在基线库上证明闸门可 FAIL")
    ap.add_argument("--template", default=lab.DEFAULT_TEMPLATE)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
