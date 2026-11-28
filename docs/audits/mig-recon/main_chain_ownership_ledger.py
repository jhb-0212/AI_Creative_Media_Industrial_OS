"""正式链（main 迁移链）归属验收台账 —— 生产准入 D2 的独立取证。

为什么不能复用 `acceptance-ledger.md` 的结论
--------------------------------------------
那份台账在分叉库 `ai_film_industrial` 上采集（35/35 闭环），而分叉库与正式链
**不是同一套谱系**：分叉库有 `generation_execution` / `shot_planning` /
`workflow` 等 schema 与三列 `(project_id, tenant_id, organization_id)` 外键；
正式链把这些域合并进 `production_planning` / `budget_quota` / `qc_governance`，
且**子表不存 `organization_id`**（组织归属的单一事实源在 `projects`）。

ADR-R41 D5 明文禁止跨链挪用证据，因此本脚本对正式链**重新派生**目标集合。

判据
----
    项目真实(t)   ⇔ 直接外键指向 projects（键含 project_id）
                     ∨ 存在复合外键指向某父表 p，键含 project_id 且 项目真实(p)
    租户一致(t)   ⇔ 存在指向 projects 的外键，且该外键覆盖 tenant_id
    数据干净(t)   ⇔ 孤儿行 = 0 ∧ 租户不一致行 = 0

    PASS(t) ⇔ 项目真实(t) ∧ 租户一致(t) ∧ 数据干净(t) ∧ 相关外键已 VALIDATE

「项目真实」为什么必须走**传递闭包**
------------------------------------
`scenes` 没有直接指向 projects 的外键，但有 `(project_id, episode_id) →
episodes(project_id, entity_id)`，而 episodes 的 project_id 指 projects。
于是 scenes.project_id 必然真实存在——判成「无外键」会误导人去加一条冗余外键，
而冗余列正是本项目一直在清的东西。反过来，闭包也挡住循环引用（A→B、B→A 且
谁也不指向 projects 时两者都不算真实）。

「租户一致」为什么不参与闭包
----------------------------
父链只保证「同一个 project_id」，**推不出**「同一行的 tenant_id 等于该项目
的租户」。补约束之前这一点是实测可违反的（首轮 ``--selftest`` 插入一行租户
不一致的 episode 被接受，违规计数变成 1）。

``c9d0e1f2a3b4`` 之后 13 张表已挂 ``(project_id, tenant_id) -> projects``
复合外键，同一行写入会以 23503 被拒。但本判据**仍然独立存在**：租户一致是
归属真实性的另一个事实，不是「项目真实」的推论——将来若有表只挂了单列外键，
闭包会判它「项目真实 = 是」，而租户一致仍是「否」。把它并进闭包，就等于让
一个维度替另一个维度作证。

用法::

    python docs/audits/mig-recon/main_chain_ownership_ledger.py --db ai_film_api_tests --port 15444
    python docs/audits/mig-recon/main_chain_ownership_ledger.py --selftest --db ai_film_api_tests
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import asyncpg

OUT_DIR = Path(__file__).resolve().parent / "main-chain"
OUT_MD = OUT_DIR / "ownership-ledger.md"
OUT_JSON = OUT_DIR / "ownership-ledger.json"

PASS = "PASS"
GAP_TENANT_UNCOVERED = "GAP_TENANT_UNCOVERED"
FAIL_NO_PATH = "FAIL_NO_PATH"
FAIL_WEAK_PATH = "FAIL_WEAK_PATH"
FAIL_NOT_VALIDATED = "FAIL_NOT_VALIDATED"
FAIL_DIRTY = "FAIL_DIRTY"


@dataclass(frozen=True)
class Edge:
    """一条外键边（只用于归属推理，不含业务语义）。

    ``strong`` 的含义：参与外键的**所有子表列都是 NOT NULL**。
    复合外键在任一列为 NULL 时**完全不校验**（SQL 标准的 MATCH SIMPLE），
    因此「可空列参与的外键」对归属只提供弱保证——`project_id` 可以在
    `episode_id IS NULL` 时指向任意字符串。把弱路径当强路径会让台账给出
    过度乐观的结论，比不判更糟。
    """

    constraint: str
    child: str
    parent: str
    child_cols: tuple[str, ...]
    parent_cols: tuple[str, ...]
    validated: bool
    strong: bool


@dataclass(frozen=True)
class TableRow:
    child: str
    has_project_id: bool
    has_tenant_id: bool
    direct_fk: str | None
    project_reachable: bool
    reach_path: str
    fk_covers_tenant: bool
    validated: bool | None
    orphan_rows: int
    tenant_mismatch_rows: int
    verdict: str
    reason: str


@dataclass(frozen=True)
class ConsistencyRow:
    child: str
    parent: str
    constraint: str
    columns: str
    validated: bool
    dangling_rows: int
    verdict: str


async def _find_schema(conn: asyncpg.Connection, table: str) -> str:
    """定位核心表的 schema，不写死。多个同名表时报错而不是挑一个。"""
    rows = await conn.fetch(
        """
        SELECT n.nspname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = $1 AND c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        """,
        table,
    )
    schemas = [r["nspname"] for r in rows]
    if not schemas:
        raise SystemExit(f"正式链上找不到表 {table!r}：库可能未迁移到 head")
    if len(schemas) > 1:
        raise SystemExit(f"表 {table!r} 出现在多个 schema {schemas}，需人工确认归属模型")
    return schemas[0]


async def _columns(conn: asyncpg.Connection, rel: str) -> dict[str, int]:
    rows = await conn.fetch(
        """
        SELECT a.attname, a.attnum FROM pg_attribute a
        WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
        """,
        rel,
    )
    return {str(r["attname"]): int(r["attnum"]) for r in rows}


async def _colnames(conn: asyncpg.Connection, rel: str, attnums: list[int]) -> list[str]:
    """按 attnum 原顺序还原列名。

    顺序不能错：``(project_id, episode_id)`` 与 ``(episode_id, project_id)``
    生成的 JOIN 条件列对应关系不同，错位会给出一个「比对了错的两列」的 PASS。
    """
    rows = await conn.fetch(
        """
        SELECT a.attnum, a.attname FROM pg_attribute a
        WHERE a.attrelid = $1::regclass AND a.attnum = ANY($2::int2[])
        """,
        rel,
        attnums,
    )
    by_num = {int(r["attnum"]): str(r["attname"]) for r in rows}
    missing = [n for n in attnums if n not in by_num]
    if missing:
        raise SystemExit(f"{rel} 的 attnum {missing} 找不到列名")
    return [by_num[n] for n in attnums]


async def _edges(conn: asyncpg.Connection) -> list[Edge]:
    rows = await conn.fetch(
        """
        SELECT con.conname, con.convalidated,
               con.conrelid::regclass::text AS child,
               con.confrelid::regclass::text AS parent,
               con.conkey, con.confkey
        FROM pg_constraint con
        WHERE con.contype = 'f'
        ORDER BY 3, 1
        """
    )
    out: list[Edge] = []
    for row in rows:
        child, parent = str(row["child"]), str(row["parent"])
        child_keys = [int(k) for k in row["conkey"]]
        child_cols = tuple(await _colnames(conn, child, child_keys))
        not_null = await conn.fetch(
            """
            SELECT a.attname FROM pg_attribute a
            WHERE a.attrelid = $1::regclass AND a.attnum = ANY($2::int2[])
              AND a.attnotnull
            """,
            child,
            child_keys,
        )
        strong = {str(r["attname"]) for r in not_null} == set(child_cols)
        out.append(
            Edge(
                constraint=str(row["conname"]),
                child=child,
                parent=parent,
                child_cols=child_cols,
                parent_cols=tuple(
                    await _colnames(conn, parent, [int(k) for k in row["confkey"]])
                ),
                validated=bool(row["convalidated"]),
                strong=strong,
            )
        )
    return out


def _reachable_tables(
    edges: list[Edge], projects: str
) -> tuple[dict[str, str], dict[str, str]]:
    """不动点求「项目真实」可达表，返回 ``(强可达, 弱可达)``，值为依据说明。

    强可达只允许全程 NOT NULL 的边；弱可达额外允许含可空列的边。
    先收敛强集再算弱集：弱路径若挂在一张只是弱可达的父表上，它仍然是弱的。
    """

    def _direct(edge: Edge) -> bool:
        return edge.parent == projects and "project_id" in edge.child_cols

    def _inherit(edge: Edge) -> bool:
        return (
            "project_id" in edge.child_cols and "project_id" in edge.parent_cols
        )

    strong: dict[str, str] = {}
    for edge in edges:
        if _direct(edge) and edge.strong:
            strong[edge.child] = f"直接 ← {edge.constraint}"
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge.child in strong or not _inherit(edge) or not edge.strong:
                continue
            if edge.parent in strong:
                strong[edge.child] = f"经 {edge.parent}（{edge.constraint}）"
                changed = True

    weak: dict[str, str] = dict(strong)
    for edge in edges:
        if _direct(edge) and edge.child not in weak:
            weak[edge.child] = f"直接 ← {edge.constraint}（列可空）"
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge.child in weak or not _inherit(edge):
                continue
            if edge.parent in strong:
                weak[edge.child] = f"经 {edge.parent}（{edge.constraint}，列可空）"
                changed = True
            elif edge.parent in weak:
                weak[edge.child] = f"经 {edge.parent}（{edge.constraint}，弱链）"
                changed = True
    return strong, weak


async def _collect(
    conn: asyncpg.Connection, projects: str
) -> tuple[list[TableRow], list[ConsistencyRow]]:
    edges = await _edges(conn)
    strong, weak = _reachable_tables(edges, projects)

    candidates = await conn.fetch(
        """
        SELECT c.table_schema, c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables tb
          ON tb.table_schema = c.table_schema
         AND tb.table_name = c.table_name
        WHERE c.column_name = 'project_id'
          AND tb.table_type = 'BASE TABLE'
          AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY c.table_schema, c.table_name
        """
    )

    tables: list[TableRow] = []
    for row in candidates:
        child = f"{row['table_schema']}.{row['table_name']}"
        cols = await _columns(conn, child)
        has_tenant = "tenant_id" in cols

        direct = next(
            (
                e
                for e in edges
                if e.child == child and e.parent == projects and "project_id" in e.child_cols
            ),
            None,
        )
        covers_tenant = bool(direct) and has_tenant and "tenant_id" in direct.child_cols

        orphan = int(
            await conn.fetchval(
                f"""
                SELECT count(*) FROM {child} c
                WHERE c.project_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM {projects} p WHERE p.entity_id = c.project_id
                  )
                """
            )
        )
        mismatch = 0
        if has_tenant:
            mismatch = int(
                await conn.fetchval(
                    f"""
                    SELECT count(*) FROM {child} c
                    JOIN {projects} p ON p.entity_id = c.project_id
                    WHERE c.tenant_id <> p.tenant_id
                    """
                )
            )

        verdict, reason = _judge(
            direct=direct,
            strong=child in strong,
            weak=child in weak,
            covers_tenant=covers_tenant,
            has_tenant=has_tenant,
            orphan=orphan,
            mismatch=mismatch,
        )
        tables.append(
            TableRow(
                child=child,
                has_project_id=True,
                has_tenant_id=has_tenant,
                direct_fk=direct.constraint if direct else None,
                project_reachable=child in strong or child in weak,
                reach_path=strong.get(child) or weak.get(child, "—"),
                fk_covers_tenant=covers_tenant,
                validated=direct.validated if direct else None,
                orphan_rows=orphan,
                tenant_mismatch_rows=mismatch,
                verdict=verdict,
                reason=reason,
            )
        )

    consistency = await _consistency(conn, edges, projects)
    return tables, consistency


def _judge(
    *,
    direct: Edge | None,
    strong: bool,
    weak: bool,
    covers_tenant: bool,
    has_tenant: bool,
    orphan: int,
    mismatch: int,
) -> tuple[str, str]:
    if not weak:
        return (
            FAIL_NO_PATH,
            "既无指向 projects 的外键，也无法经复合父链到达 projects："
            "project_id 可以指向任意字符串",
        )
    if not strong:
        return (
            FAIL_WEAK_PATH,
            "到 projects 的路径含可空列：复合外键在任一列为 NULL 时完全不校验，"
            "project_id 此时可指向任意字符串",
        )
    if direct is not None and not direct.validated:
        return FAIL_NOT_VALIDATED, "外键存在但未 VALIDATE：历史脏行被永久豁免"
    if orphan > 0:
        return FAIL_DIRTY, f"存在 {orphan} 行引用了不存在的父行"
    if has_tenant and not covers_tenant:
        return (
            GAP_TENANT_UNCOVERED,
            "项目真实但 tenant_id 未被任何外键约束："
            "本行租户可与父项目租户不一致（实测可写入）",
        )
    if mismatch > 0:
        return FAIL_DIRTY, f"存在 {mismatch} 行 tenant_id 与父项目不一致"
    return PASS, "项目真实 ∧ 租户一致 ∧ 违规行数 0"


async def _consistency(
    conn: asyncpg.Connection, edges: list[Edge], projects: str
) -> list[ConsistencyRow]:
    """「一致」半边：复合外键保证本行 project_id 等于父行的 project_id。"""
    out: list[ConsistencyRow] = []
    for edge in edges:
        if len(edge.child_cols) < 2 or edge.parent == projects:
            continue
        if "project_id" not in edge.child_cols or "project_id" not in edge.parent_cols:
            continue
        cond = " AND ".join(
            f"c.{a} = p.{b}"
            for a, b in zip(edge.child_cols, edge.parent_cols, strict=True)
        )
        not_null = " AND ".join(f"c.{a} IS NOT NULL" for a in edge.child_cols)
        dangling = int(
            await conn.fetchval(
                f"""
                SELECT count(*) FROM {edge.child} c
                LEFT JOIN {edge.parent} p ON {cond}
                WHERE {not_null} AND p.{edge.parent_cols[0]} IS NULL
                """
            )
        )
        verdict = (
            FAIL_NOT_VALIDATED
            if not edge.validated
            else (FAIL_DIRTY if dangling else PASS)
        )
        out.append(
            ConsistencyRow(
                child=edge.child,
                parent=edge.parent,
                constraint=edge.constraint,
                columns=", ".join(
                    f"{a}={b}"
                    for a, b in zip(edge.child_cols, edge.parent_cols, strict=True)
                ),
                validated=edge.validated,
                dangling_rows=dangling,
                verdict=verdict,
            )
        )
    return out


def _render_md(
    *, db: str, port: int, tables: list[TableRow], consistency: list[ConsistencyRow]
) -> str:
    passed = [t for t in tables if t.verdict == PASS]
    c_passed = [c for c in consistency if c.verdict == PASS]
    overall = (
        "PASS"
        if len(passed) == len(tables) and len(c_passed) == len(consistency)
        else "FAIL"
    )

    lines = [
        "# 正式链归属验收台账（机器生成，勿手改）",
        "",
        f"- 目标库：`{db}` @ `localhost:{port}`（**main 正式链**，非分叉库）",
        "- 判据：`PASS ⇔ 项目真实 ∧ 租户一致 ∧ 数据干净 ∧ 相关外键已 VALIDATE`",
        "- 目标集合：从 `information_schema` / `pg_constraint` **实读派生**，不抄清单",
        f"- **整体判定：{overall}**",
        f"  - 「真实」+「租户一致」：{len(passed)}/{len(tables)} 通过",
        f"  - 「一致」（复合外键同项目）：{len(c_passed)}/{len(consistency)} 通过",
        "",
        "> 与分叉库台账（`acceptance-ledger.md`，35/35）不可互推：两库谱系不同。",
        "> 分叉库是「三列外键 + 冗余 organization_id」；正式链是「两列外键 + 组织只在 projects」。",
        "> ADR-R41 D5 禁止跨链挪用证据。",
        "",
        "## 一、「真实」与「租户一致」",
        "",
        "| # | 表 | tenant_id | 直接外键 | 项目真实可达 | 依据 | 覆盖 tenant | validated | 孤儿 | 租户不一致 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, t in enumerate(tables, 1):
        lines.append(
            f"| {i} | `{t.child}` | {'是' if t.has_tenant_id else '否'} | "
            f"{('`' + t.direct_fk + '`') if t.direct_fk else '**无**'} | "
            f"{'是' if t.project_reachable else '**否**'} | {t.reach_path} | "
            f"{'是' if t.fk_covers_tenant else '否'} | "
            f"{'-' if t.validated is None else str(t.validated).lower()} | "
            f"{t.orphan_rows} | {t.tenant_mismatch_rows} | {t.verdict} |"
        )

    gaps = [t for t in tables if t.verdict != PASS]
    if gaps:
        lines += ["", "### 未通过项与原因", ""]
        for t in gaps:
            lines.append(f"- `{t.child}` — **{t.verdict}**：{t.reason}")

    lines += [
        "",
        "## 二、「一致」半边：复合外键保证本行与父行同项目",
        "",
        "| # | 子表 | 父表 | 约束 | 列映射 | validated | 悬空行 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(consistency, 1):
        lines.append(
            f"| {i} | `{c.child}` | `{c.parent}` | `{c.constraint}` | "
            f"`{c.columns}` | {str(c.validated).lower()} | {c.dangling_rows} | {c.verdict} |"
        )

    lines += [
        "",
        "## 三、可复算的复验命令",
        "",
        "```sql",
        "-- 租户一致性（以 episodes 为例，任一张含 tenant_id 的表同理）",
        "SELECT count(*) FROM project_planning.episodes e",
        "  JOIN project_planning.projects p ON p.entity_id = e.project_id",
        " WHERE e.tenant_id <> p.tenant_id;",
        "",
        "-- 孤儿行（无任何归属路径的表才会 > 0）",
        "SELECT count(*) FROM qc_governance.qc_tasks q",
        " WHERE NOT EXISTS (",
        "   SELECT 1 FROM project_planning.projects p WHERE p.entity_id = q.project_id);",
        "```",
        "",
        "## 四、自检",
        "",
        "`--selftest` 会插入一行「project_id 指向租户 A 的项目、tenant_id 写租户 B」的",
        "episode，然后回滚。它必须使「租户不一致行数」≥ 1；否则说明本台账的判定是恒真的。",
        "",
    ]
    return "\n".join(lines)


async def _tenant_fk_name(conn: asyncpg.Connection, rel: str, projects: str) -> str:
    """取 rel 上「(project_id, tenant_id) -> projects」的约束名，**不写死**。

    名字写死会让自检在约束被重命名后报出「找不到约束」，而那看起来跟「约束
    真的不存在」一模一样——自检自己变成一个假信号源。列集从 catalog 实读，
    与迁移里的派生命名互为校验。
    """
    rows = await conn.fetch(
        """
        SELECT con.conname,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute a
                  ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS cols
        FROM pg_constraint con
        WHERE con.contype = 'f'
          AND con.conrelid = $1::regclass
          AND con.confrelid = $2::regclass
        """,
        rel,
        projects,
    )
    for row in rows:
        if sorted(row["cols"]) == ["project_id", "tenant_id"]:
            return str(row["conname"])
    raise SystemExit(
        f"{rel} 上没有覆盖 (project_id, tenant_id) 的归属外键：先跑迁移 c9d0e1f2a3b4"
    )


_SELFTEST_INSERT = """
    INSERT INTO project_planning.episodes
      (entity_id, entity_type, schema_version, entity_version, status,
       owner_module, created_at, created_by, updated_at, project_id,
       episode_ref, title, tenant_id)
    VALUES ('selftest-tenant-mismatch', 'Episode', 205, 1, 'DRAFT',
            'project_planning', now(), 'selftest', now(), $1,
            'EP-SELFTEST', '自检探针', $2)
"""

_SELFTEST_COUNT = """
    SELECT count(*) FROM project_planning.episodes e
    JOIN project_planning.projects p ON p.entity_id = e.project_id
    WHERE e.tenant_id <> p.tenant_id
"""


async def _selftest(conn: asyncpg.Connection, projects: str) -> int:
    """证明这条判据能 FAIL——两侧各证伪一次，缺一侧就是假阳性。

    修复前（``c9d0e1f2a3b4`` 之前）的写法是「插入租户不一致的行，计数须变 1」。
    约束补上之后，同一行**写不进去**了，旧写法会以「计数 = 0」失败——而那会被
    误读成「判据失效」。所以判据本身要跟着事实升级，而不是把失败解释掉。

    A. **约束在位**：同一行必须被外键拒绝，且 SQLSTATE 恰为 23503。
       只断言「抛异常」不够——RLS（42501）、check（23514）、列类型错都可能抛，
       它们证明的是别的东西在拦，不是归属约束。
    B. **摘掉约束**（事务内，最后回滚）：同一行必须能写入，且违规计数查询
       返回 ≥ 1。这一侧证明的是「计数不是恒 0」——否则 A 通过 + 计数恒 0
       会让台账永远绿，而绿的原因是它算不出来。

    两步在同一个事务里：``tr.rollback()`` 把 ``DROP CONSTRAINT`` 与插入一并
    撤销，所以自检对库不留痕迹（PostgreSQL 的 DDL 是事务性的）。
    """
    project = await conn.fetchrow(f"SELECT entity_id, tenant_id FROM {projects} LIMIT 1")
    if project is None:
        raise SystemExit("自检需要至少一个项目：请先在目标库播种")
    other_tenant = "11111111-1111-1111-1111-111111111111"
    rel = "project_planning.episodes"
    fk_name = await _tenant_fk_name(conn, rel, projects)

    rejected_by_fk = False
    counted = -1

    tr = conn.transaction()
    await tr.start()
    try:
        # A. 约束在位：写入必须被外键拒绝（嵌套 transaction 即 SAVEPOINT，
        #    失败只回滚到存盘点，不会把外层事务打成 aborted）。
        try:
            async with conn.transaction():
                await conn.execute(_SELFTEST_INSERT, project["entity_id"], other_tenant)
        except asyncpg.ForeignKeyViolationError:
            rejected_by_fk = True

        # B. 摘掉约束：同一行必须能写入，且计数必须发现它。
        await conn.execute(f'ALTER TABLE {rel} DROP CONSTRAINT "{fk_name}"')
        await conn.execute(_SELFTEST_INSERT, project["entity_id"], other_tenant)
        counted = int(await conn.fetchval(_SELFTEST_COUNT))
    finally:
        await tr.rollback()

    print(f"selftest A: 约束在位时写入被外键拒绝 = {rejected_by_fk}（期望 True，23503）")
    print(f"selftest B: 摘除约束后违规行计数 = {counted}（期望 ≥ 1）")
    ok = rejected_by_fk and counted >= 1
    print("selftest: " + ("通过（判据两侧均可证伪）" if ok else "**失败**"))
    return 0 if ok else 1


async def main_async(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.db,
    )
    try:
        projects = f"{await _find_schema(conn, 'projects')}.projects"
        if args.selftest:
            return await _selftest(conn, projects)

        tables, consistency = await _collect(conn, projects)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(
            json.dumps(
                {
                    "database": args.db,
                    "host": args.host,
                    "port": args.port,
                    "projects": projects,
                    "tables": [asdict(t) for t in tables],
                    "consistency": [asdict(c) for c in consistency],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        OUT_MD.write_text(
            _render_md(db=args.db, port=args.port, tables=tables, consistency=consistency),
            encoding="utf-8",
        )
        failed = [t for t in tables if t.verdict != PASS]
        print(f"台账已生成：{OUT_MD}")
        print(f"未通过 {len(failed)}/{len(tables)} 张表")
        return 0 if not failed else 1
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="正式链归属验收台账（D2）")
    ap.add_argument("--db", default="ai_film_api_tests")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument(
        "--password", default=os.environ.get("POSTGRES_PASSWORD", "<ROTATED-2026-11-28>")
    )
    ap.add_argument("--selftest", action="store_true", help="证明判定可 FAIL")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
