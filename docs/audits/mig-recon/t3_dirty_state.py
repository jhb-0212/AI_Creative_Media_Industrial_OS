"""T3：归属约束在「脏数据」前置状态下的行为验证。

设计原则（为什么这样写）：

1. **一条语句一个步骤**。多语句在一个隐式事务里执行，中途失败会把前面的 `DROP CONSTRAINT`
   一起回滚，导致后续场景在「以为约束已撤」的状态下运行——这是最容易被忽略的假阳性来源。

2. **逐语句捕获 SQLSTATE**。`23503` 外键违反、`42830` 被引用表缺唯一约束、`23502` 非空违反、
   `P0001` plpgsql `RAISE EXCEPTION`、`42710` 重复对象。把期望值写成断言，实验才可判定。
   非 `PostgresError` 的异常单独记为 `EXC`，不允许整个脚本中断——上一轮 `aborted` 就是
   `reseed()` 里 TRUNCATE 一张尚不存在的表抛 `UndefinedTableError` 直接穿透 main()。

3. **每次快照含约束与触发器状态**（`convalidated` 的 t/f、`tgenabled` 的 O/D）。否则
   「NOT VALID 生效」「守卫已临时关闭」只能靠人记，而这两者正是本实验要证明的东西。

4. **数据保全可判定**：每条语句前后取 `count(*) + 行级 md5`，任何静默删改都会改变哈希。

5. **修复路径必须如实记录**。分支侧 `trg_asset_versions_guard` 把 `project_id` /
   `organization_id` 列为不可变内容列，就地 `UPDATE` 会被 `P0001` 拒绝。这是真实结论，
   不是脚本缺陷——脚本先断言「就地修复被拒」，再走维护窗口（关闭守卫 → UPDATE → 重新启用）
   验证修复后可 `VALIDATE`。

6. **在克隆库 `t3_lab` 上做**（模板 `ai_film_industrial`，step-feat 集群已重放到分支 head），
   step-feat 自身保留为对照。

用法：
    python docs/audits/mig-recon/t3_dirty_state.py
输出：
    docs/audits/mig-recon/t3-dirty-state.md
    docs/audits/mig-recon/t3-dirty-state.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

HOST = "localhost"
PORT = 15444  # step-feat：分支链已重放到 head
MAIN_PORT = 15443  # step-main：主支链 head，用于可空性对照
SUPER_PW = "replay_super_pw"
LAB = "t3_lab"
TEMPLATE = "ai_film_industrial"
MAIN_DB = "ai_film_industrial"

ASSETS = "asset_service.asset_versions"
PROJECTS = "project_planning.projects"
ORGS = "project_planning.organizations"
FK_2COL = "fk_asset_versions_project_tenant"
FK_3COL = "fk_asset_versions_project_tenant_org"
TRG_ASSERT = "asset_versions_project_assigned"
TRG_GUARD = "trg_asset_versions_guard"

T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"
O1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
O2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOPE = "ffffffff-ffff-ffff-ffff-ffffffffffff"  # 不存在的组织
P1 = "proj-1"

CLEANUP_NOTE = "TRUNCATE 不触发行级触发器，故可作为阶段重置；守卫拦不住 TRUNCATE，属运维权限面的边界"


def asset_insert(
    entity_id: str,
    *,
    tenant: str = T1,
    org: str = O1,
    project: str | None = P1,
    tone: str = "a",
) -> str:
    """构造资产插入语句；`project=None` 生成 SQL NULL（验证非空是否挡住不完整归属）。"""
    project_sql = "NULL" if project is None else f"'{project}'"
    return (
        f"INSERT INTO {ASSETS} (entity_id, entity_type, schema_version, entity_version, status, "
        f"owner_module, tenant_id, organization_id, project_id, created_at, created_by, updated_at, "
        f"asset_type, sha256, storage_ref, source_asset_version_ids) VALUES "
        f"('{entity_id}', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service', "
        f"'{tenant}', '{org}', {project_sql}, now(), 't3', now(), "
        f"'VIDEO', repeat('{tone}', 64), "
        f"'{{\"bucket\":\"b\",\"key\":\"{entity_id}\"}}'::jsonb, '[]'::jsonb)"
    )


def add_fk2(*, not_valid: bool = False) -> str:
    suffix = " NOT VALID" if not_valid else ""
    return (
        f"ALTER TABLE {ASSETS} ADD CONSTRAINT {FK_2COL} FOREIGN KEY (project_id, tenant_id) "
        f"REFERENCES {PROJECTS}(entity_id, tenant_id){suffix}"
    )


def add_fk3() -> str:
    return (
        f"ALTER TABLE {ASSETS} ADD CONSTRAINT {FK_3COL} "
        f"FOREIGN KEY (project_id, tenant_id, organization_id) "
        f"REFERENCES {PROJECTS}(entity_id, tenant_id, organization_id)"
    )


def drop_both() -> str:
    return (
        f"ALTER TABLE {ASSETS} DROP CONSTRAINT IF EXISTS {FK_2COL}, "
        f"DROP CONSTRAINT IF EXISTS {FK_3COL}"
    )


@dataclass
class Step:
    phase: str
    label: str
    sql: str
    expect: str | None = None  # None = 期望成功
    note: str = ""


@dataclass
class Result:
    phase: str
    label: str
    sql: str
    expect: str
    actual: str
    detail: str
    rows_before: int
    rows_after: int
    hash_before: str
    hash_after: str
    constraints_before: str
    constraints_after: str
    note: str
    verdict: str


async def snapshot(conn: asyncpg.Connection) -> tuple[int, str, str]:
    row = await conn.fetchrow(
        f"SELECT count(*) AS n, coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') AS h "
        f"FROM {ASSETS} t"
    )
    assert row is not None
    cs = await conn.fetch(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1::text[]) ORDER BY conname",
        [FK_2COL, FK_3COL],
    )
    cmap = {r["conname"]: ("t" if r["convalidated"] else "f") for r in cs}
    ts = await conn.fetch(
        "SELECT tgname, (tgenabled::text IN ('O', 'A')) AS enabled FROM pg_trigger "
        "WHERE tgrelid = $1::regclass AND tgname = ANY($2::text[]) ORDER BY tgname",
        ASSETS,
        [TRG_ASSERT, TRG_GUARD],
    )
    # tgenabled 是内部 "char"，客户端解码后与 'O' 比较不可靠，故由 SQL 侧归一为布尔。
    tmap = {r["tgname"]: ("O" if r["enabled"] else "D") for r in ts}
    label = (
        f"fk2={cmap.get(FK_2COL, '-')} fk3={cmap.get(FK_3COL, '-')} "
        f"assert={tmap.get(TRG_ASSERT, '-')} guard={tmap.get(TRG_GUARD, '-')}"
    )
    return int(row["n"]), str(row["h"])[:8], label


async def run(conn: asyncpg.Connection, step: Step) -> Result:
    n0, h0, c0 = await snapshot(conn)
    actual, detail = "OK", ""
    try:
        await conn.execute(step.sql)
    except asyncpg.PostgresError as exc:
        actual = str(exc.sqlstate)
        detail = str(exc).strip().splitlines()[0][:220]
    except Exception as exc:  # 兜底记录，绝不让脚本中断
        actual = "EXC"
        detail = f"{type(exc).__name__}: {exc}"[:220]
    n1, h1, c1 = await snapshot(conn)
    ok = (step.expect is None and actual == "OK") or actual == step.expect
    result = Result(
        phase=step.phase,
        label=step.label,
        sql=" ".join(step.sql.split())[:400],
        expect=step.expect or "成功",
        actual=actual,
        detail=detail,
        rows_before=n0,
        rows_after=n1,
        hash_before=h0,
        hash_after=h1,
        constraints_before=c0,
        constraints_after=c1,
        note=step.note,
        verdict="PASS" if ok else "FAIL",
    )
    print(f"[{result.verdict}] {step.phase} {step.label}: 期望={result.expect} 实际={actual} | {c1}")
    return result


async def reset_base(conn: asyncpg.Connection) -> None:
    """实验起点：只清空随实验变化的表（模板库中两张表均 0 行）。

    `organizations` 尚未建表，故此处不列——这是上一轮 `aborted` 的根因。
    """
    await conn.execute(drop_both())
    await conn.execute(f"TRUNCATE {ASSETS}, {PROJECTS} CASCADE")


def build_steps() -> list[Step]:
    steps: list[Step] = []
    add = steps.append

    # ===== A 前置：目标键是否存在 =====
    add(
        Step(
            "A0",
            "分支底座直接建两列外键",
            add_fk2(),
            "42830",
            "被引用侧无 (entity_id, tenant_id) 唯一约束 → 约束物理不可建；是目标键缺失，不是数据脏",
        )
    )
    add(
        Step(
            "A1",
            "补 organizations 表（镜像 main 的 DDL）",
            f"CREATE TABLE {ORGS} (organization_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            f"name varchar NOT NULL, created_at timestamptz NOT NULL, created_by varchar NOT NULL, "
            f"updated_at timestamptz NOT NULL, "
            f"CONSTRAINT uq_organizations_owner_tenant UNIQUE (organization_id, tenant_id))",
            note="分支链整张表不存在（阻塞 B1 与 L2→L1 外键）",
        )
    )
    add(
        Step(
            "A2",
            "写入两个组织（T1/O1、T2/O2）",
            f"INSERT INTO {ORGS} (organization_id, tenant_id, name, created_at, created_by, updated_at) VALUES "
            f"('{O1}', '{T1}', 'Org One', now(), 't3', now()), "
            f"('{O2}', '{T2}', 'Org Two', now(), 't3', now())",
        )
    )
    add(
        Step(
            "A3",
            "projects 补 (entity_id, tenant_id) 唯一键",
            f"ALTER TABLE {PROJECTS} ADD CONSTRAINT uq_projects_entity_tenant UNIQUE (entity_id, tenant_id)",
            note="阻塞 A8；主支已有同名约束",
        )
    )
    add(
        Step(
            "A4",
            "projects 补三列唯一键",
            f"ALTER TABLE {PROJECTS} ADD CONSTRAINT uq_projects_entity_tenant_org "
            f"UNIQUE (entity_id, tenant_id, organization_id)",
            note="三列外键的被引用目标；分支 projects.organization_id 为 NOT NULL",
        )
    )
    add(
        Step(
            "A5",
            "projects → organizations 复合外键",
            f"ALTER TABLE {PROJECTS} ADD CONSTRAINT fk_projects_organization_tenant "
            f"FOREIGN KEY (organization_id, tenant_id) REFERENCES {ORGS}(organization_id, tenant_id)",
            note="打通 L2→L1；分支侧两列均 NOT NULL，故 MATCH SIMPLE 无放行口",
        )
    )
    add(
        Step(
            "A6",
            "写入项目 P1（属 T1/O1）",
            f"INSERT INTO {PROJECTS} (entity_id, entity_type, schema_version, entity_version, status, "
            f"owner_module, created_at, created_by, updated_at, project_name, project_tier, "
            f"tenant_id, organization_id) VALUES "
            f"('{P1}', 'Project', 2, 1, 'ACTIVE', 'project_planning', now(), 't3', now(), "
            f"'Project One', 'STANDARD', '{T1}', '{O1}')",
        )
    )

    # ===== B 场景 1：正常归属 =====
    add(Step("B1", "插入项目/租户/组织完全匹配的资产", asset_insert("asset-ok"), note="场景 1 正向"))
    add(
        Step(
            "B2",
            "干净数据下建两列外键",
            add_fk2(),
            note="补齐前置后可建 → 反证 A0 的 42830 是目标键缺失，不是数据脏",
        )
    )

    # ===== C 场景 2 + 7：孤儿项目 / NOT VALID 分阶段 =====
    add(Step("C1", "撤约束以注入脏数据", drop_both(), note="历史库通常无约束，脏数据正由此产生"))
    add(
        Step(
            "C2",
            "注入孤儿资产（project 不存在）",
            asset_insert("asset-orphan", project="ghost-project", tone="b"),
            note="场景 2：无约束时孤儿写入成功——归属事实链断裂的实证",
        )
    )
    add(
        Step(
            "C3",
            "含孤儿时直接建两列外键",
            add_fk2(),
            "23503",
            note="外键预检识别孤儿并拒绝，且不静默删数据（行数不变）",
        )
    )
    add(
        Step(
            "C4",
            "改用 NOT VALID 分阶段添加",
            add_fk2(not_valid=True),
            note="场景 7：NOT VALID 跳过历史扫描，新写入仍受约束",
        )
    )
    add(
        Step(
            "C5",
            "NOT VALID 下再写孤儿",
            asset_insert("asset-orphan-2", project="ghost-project-2", tone="c"),
            "23503",
            note="场景 7 关键：历史脏数据未清时，新写入同样被拦",
        )
    )
    add(Step("C6", "NOT VALID 下写合规资产", asset_insert("asset-ok-2", tone="d"), note="合规写入不被误伤"))
    add(
        Step(
            "C7",
            "脏数据未修就 VALIDATE",
            f"ALTER TABLE {ASSETS} VALIDATE CONSTRAINT {FK_2COL}",
            "23503",
            note="NOT VALID 不是长期豁免；失败后约束保持 convalidated=f",
        )
    )
    add(
        Step(
            "C8",
            "就地修复：孤儿映射到真实项目",
            f"UPDATE {ASSETS} SET project_id = '{P1}' WHERE entity_id = 'asset-orphan'",
            "P0001",
            note="**分支结构事实**：project_id 属不可变内容列，就地 UPDATE 被守卫拒绝（ADR-R11）",
        )
    )
    add(
        Step(
            "C9",
            "维护窗口：临时关闭内容守卫",
            f"ALTER TABLE {ASSETS} DISABLE TRIGGER {TRG_GUARD}",
            note="单语句：即使后续失败也不会把守卫留在关闭态（下一阶段重置会重建）",
        )
    )
    add(
        Step(
            "C10",
            "守卫关闭期间执行同一修复",
            f"UPDATE {ASSETS} SET project_id = '{P1}' WHERE entity_id = 'asset-orphan'",
            note="同一语句、同一数据，仅守卫状态不同 → 证明 C8 的 P0001 唯一来源是守卫",
        )
    )
    add(
        Step(
            "C11",
            "重新启用内容守卫",
            f"ALTER TABLE {ASSETS} ENABLE TRIGGER {TRG_GUARD}",
        )
    )
    add(
        Step(
            "C12",
            "修复后 VALIDATE",
            f"ALTER TABLE {ASSETS} VALIDATE CONSTRAINT {FK_2COL}",
            note="约束转为已验证（fk2=t）",
        )
    )

    # ===== D 场景 3：租户错配 =====
    add(Step("D0", "撤约束", drop_both(), note=CLEANUP_NOTE))
    add(Step("D1", "清空资产表", f"TRUNCATE {ASSETS} CASCADE"))
    add(
        Step(
            "D2",
            "注入租户错配行",
            asset_insert("asset-tenant-bad", tenant=T2, org=O2, tone="e"),
            note="项目 P1 属 T1，资产写 T2；组织 O2 属 T2，故唯一违规点是 project+tenant 组合",
        )
    )
    add(Step("D3", "含租户错配时建两列外键", add_fk2(), "23503", note="场景 3：租户错配被拒"))
    add(
        Step(
            "D4",
            "就地改租户以图绕过",
            f"UPDATE {ASSETS} SET tenant_id = '{T1}' WHERE entity_id = 'asset-tenant-bad'",
            "P0001",
            note="复证：归属列全部不可就地改写，脏数据只能前向新建版本或走维护窗口",
        )
    )

    # ===== E 场景 4：组织错配（关键） =====
    add(Step("E0", "清空资产表", f"TRUNCATE {ASSETS} CASCADE", note="上一阶段未留约束"))
    add(
        Step(
            "E1",
            "注入组织错配行",
            asset_insert("asset-org-bad", org=O2, tone="f"),
            note="项目 P1 属 O1，资产写 O2；租户同为 T1",
        )
    )
    add(
        Step(
            "E2",
            "两列外键能否发现组织错配",
            add_fk2(),
            note="**预期成功**：两列只比 project+tenant → 组织错配被放行，这是两列方案的盲区",
        )
    )
    add(Step("E3", "撤两列约束", f"ALTER TABLE {ASSETS} DROP CONSTRAINT {FK_2COL}", note="准备三列方案"))
    add(
        Step(
            "E4",
            "改用三列外键",
            add_fk3(),
            "23503",
            note="三列把 organization_id 纳入比较 → 组织错配被拒",
        )
    )
    add(
        Step(
            "E5",
            "就地修复组织归属",
            f"UPDATE {ASSETS} SET organization_id = '{O1}' WHERE entity_id = 'asset-org-bad'",
            "P0001",
            note="organization_id 同样是不可变内容列",
        )
    )
    add(Step("E6", "维护窗口：关闭内容守卫", f"ALTER TABLE {ASSETS} DISABLE TRIGGER {TRG_GUARD}"))
    add(
        Step(
            "E7",
            "守卫关闭期间修复组织归属",
            f"UPDATE {ASSETS} SET organization_id = '{O1}' WHERE entity_id = 'asset-org-bad'",
            note="只改这一行，影响行数 1→1、哈希变化可见",
        )
    )
    add(Step("E8", "重新启用内容守卫", f"ALTER TABLE {ASSETS} ENABLE TRIGGER {TRG_GUARD}"))
    add(Step("E9", "修复后建三列外键", add_fk3(), note="合法数据上无副作用"))

    # ===== F 场景 5：legacy-unassigned 哨兵 =====
    add(Step("F0", "清空资产表", f"TRUNCATE {ASSETS} CASCADE", note="E9 残留的 fk3 不拦 TRUNCATE"))
    add(Step("F0b", "撤掉 E9 残留约束", drop_both(), note="隔离变量：本阶段只让触发器起作用，不让外键抢答"))
    add(
        Step(
            "F1",
            "触发器是否拦哨兵值的新写入",
            asset_insert("asset-sentinel-try", project="legacy-unassigned", tone="1"),
            "P0001",
            note="场景 5：project_id 取哨兵值 'legacy-unassigned'，分支的 assert_asset_versions_project_assigned 触发器应拒",
        )
    )
    add(
        Step(
            "F2",
            "触发器是否校验项目真实存在",
            asset_insert("asset-ghost", project="ghost-project", tone="2"),
            note="**预期成功**：只认哨兵字符串，不查存在性 → 触发器不是归属约束，只挡一个魔法值",
        )
    )
    add(
        Step(
            "F3",
            "临时关闭哨兵触发器（仅这一个）",
            f"ALTER TABLE {ASSETS} DISABLE TRIGGER {TRG_ASSERT}",
            note="单语句：即使后续插入失败，也不会把触发器留在关闭态",
        )
    )
    add(
        Step(
            "F4",
            "还原历史哨兵行",
            asset_insert("asset-sentinel-hist", project="legacy-unassigned", tone="3"),
            note="真实历史行由触发器上线前写入，此处等价复现",
        )
    )
    add(Step("F5", "维护窗口：关闭内容守卫", f"ALTER TABLE {ASSETS} DISABLE TRIGGER {TRG_GUARD}"))
    add(
        Step(
            "F6",
            "把幽灵项目行映射到真实项目",
            f"UPDATE {ASSETS} SET project_id = '{P1}' WHERE entity_id = 'asset-ghost'",
            note="幽灵行可修；哨兵行不在本步修复，留给 F9 证明必须人工决策",
        )
    )
    add(Step("F7", "重新启用内容守卫", f"ALTER TABLE {ASSETS} ENABLE TRIGGER {TRG_GUARD}"))
    add(Step("F8", "重新启用哨兵触发器", f"ALTER TABLE {ASSETS} ENABLE TRIGGER {TRG_ASSERT}"))
    add(
        Step(
            "F9",
            "含历史哨兵行时建两列外键",
            add_fk2(),
            "23503",
            note="哨兵值必须人工映射为真实项目，禁止自动当真实项目",
        )
    )

    # ===== G 场景 6：NULL 与 MATCH SIMPLE =====
    add(Step("G0", "清空资产表", f"TRUNCATE {ASSETS} CASCADE"))
    add(
        Step(
            "G1",
            "project_id 置 NULL 是否可写入",
            asset_insert("asset-null-proj", project=None, tone="4"),
            "23502",
            note="场景 6：该列 NOT NULL → 分支侧 MATCH SIMPLE 放行路径在列级即不可达",
        )
    )
    return steps


async def ownership_nullability(conn: asyncpg.Connection) -> list[dict[str, str]]:
    rows = await conn.fetch(
        "SELECT table_schema || '.' || table_name AS tbl, column_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE column_name IN ('tenant_id', 'organization_id', 'project_id') "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY 1, 2"
    )
    return [dict(r) for r in rows]


async def main_side_contrast() -> dict[str, str]:
    """主支对照：organization_id 可空 + MATCH SIMPLE ⇒ 一个组织都不存在，项目仍可写入。

    全部操作在事务内完成并 ROLLBACK，`ai_film_industrial` 不留痕迹。
    """
    conn = await asyncpg.connect(
        host=HOST, port=MAIN_PORT, user="postgres", password=SUPER_PW, database=MAIN_DB
    )
    out: dict[str, str] = {}
    tr = conn.transaction()
    await tr.start()

    async def attempt(key: str, sql: str, ok_msg: str) -> None:
        sp = conn.transaction()
        await sp.start()
        try:
            await conn.execute(sql)
            out[key] = ok_msg
            await sp.rollback()  # 结果只是判定，不落库
        except asyncpg.PostgresError as exc:
            out[key] = f"{exc.sqlstate}（{str(exc).splitlines()[0][:100]}）"
            await sp.rollback()

    try:
        out["projects.organization_id is_nullable"] = str(
            await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns WHERE "
                "table_schema = 'project_planning' AND table_name = 'projects' AND column_name = 'organization_id'"
            )
        )
        out["organizations 表行数"] = str(await conn.fetchval("SELECT count(*) FROM project_planning.organizations"))
        await attempt(
            "反证：organization_id 非空且不存在",
            "INSERT INTO project_planning.projects (entity_id, entity_type, schema_version, entity_version, "
            "status, owner_module, created_at, created_by, updated_at, project_name, project_tier, "
            f"tenant_id, organization_id) VALUES ('proj-orphan-org', 'Project', 2, 1, 'ACTIVE', "
            f"'project_planning', now(), 't3', now(), 'Orphan Org', 'STANDARD', '{T1}', '{NOPE}')",
            "成功（不应出现：说明外键未生效）",
        )
        await attempt(
            "正证：organization_id 置 NULL",
            "INSERT INTO project_planning.projects (entity_id, entity_type, schema_version, entity_version, "
            "status, owner_module, created_at, created_by, updated_at, project_name, project_tier, "
            f"tenant_id, organization_id) VALUES ('proj-null-org', 'Project', 2, 1, 'ACTIVE', "
            f"'project_planning', now(), 't3', now(), 'Null Org', 'STANDARD', '{T1}', NULL)",
            "成功（MATCH SIMPLE 跳过校验：组织表 0 行仍写入成功）",
        )
    finally:
        await tr.rollback()
        await conn.close()
    return out


SCENARIOS = [
    ("1 正常归属", "B1 → B2", "完全匹配的资产可写入，且干净数据上外键可建"),
    ("2 孤儿项目", "C2 → C3 → C10 → C12", "无约束时孤儿可写入；建约束时被拒而非静默删；修复后方可 VALIDATE"),
    ("3 租户错配", "D2 → D3", "两列外键即可拦截 project+tenant 不一致"),
    ("4 组织错配", "E1 → E2 → E4", "两列外键放行、三列外键拦截 —— 两列方案的结构性盲区"),
    ("5 legacy-unassigned 哨兵", "F1 → F2 → F9", "触发器只挡魔法值、不查存在性；历史哨兵行阻塞外键，必须人工映射"),
    ("6 NULL / MATCH SIMPLE", "G1 + 可空性审计", "分支侧归属列全 NOT NULL，放行路径列级不可达；主支 organization_id 可空则可绕过"),
    ("7 NOT VALID 分阶段", "C4 → C5 → C7 → C12", "跳过历史扫描但新写入仍受约束；未清脏即 VALIDATE 会失败且状态保持 f"),
]


async def main() -> int:
    admin = await asyncpg.connect(
        host=HOST, port=PORT, user="postgres", password=SUPER_PW, database="postgres"
    )
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{TEMPLATE}' AND pid <> pg_backend_pid()"
        )
        await admin.execute(f"DROP DATABASE IF EXISTS {LAB}")
        await admin.execute(f"CREATE DATABASE {LAB} TEMPLATE {TEMPLATE}")
    finally:
        await admin.close()

    conn = await asyncpg.connect(
        host=HOST, port=PORT, user="postgres", password=SUPER_PW, database=LAB
    )
    results: list[Result] = []
    try:
        await reset_base(conn)
        for step in build_steps():
            results.append(await run(conn, step))
        nullability = await ownership_nullability(conn)
        _, _, final_constraints = await snapshot(conn)
        final_rows = int(await conn.fetchval(f"SELECT count(*) FROM {ASSETS}"))
    finally:
        await conn.close()

    contrast = await main_side_contrast()
    passed = sum(1 for r in results if r.verdict == "PASS")

    lines = [
        "# T3 脏状态验证：原始结果（机器生成，勿手改）",
        "",
        f"- 实验库：`{LAB}`（模板 `{TEMPLATE}`，端口 {PORT}，分支链已重放到 head）",
        f"- 对照：端口 {MAIN_PORT}（主支链 head），仅用于可空性/MATCH SIMPLE 对照，事务内已 ROLLBACK",
        f"- 步骤总数 {len(results)}，PASS {passed}，FAIL {len(results) - passed}",
        "- 状态记法：`fk2=t/f` 约束已校验/未校验（f 即 NOT VALID）；`assert=O/D`、`guard=O/D` 表示"
        "哨兵触发器与内容守卫处于启用/临时关闭；`-` 表示对象不存在",
        "",
        "## 1. 七类场景覆盖",
        "",
        "| 场景 | 覆盖步骤 | 该场景要判定的事实 |",
        "| --- | --- | --- |",
    ]
    for name, steps_cov, fact in SCENARIOS:
        lines.append(f"| {name} | `{steps_cov}` | {fact} |")

    lines += [
        "",
        "## 2. 逐步结果",
        "",
        "| 阶段 | 步骤 | 期望 | 实际 SQLSTATE | 行数(前→后) | 行哈希(前→后) | 状态(前 → 后) | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.phase} | {r.label} | {r.expect} | `{r.actual}` | {r.rows_before}→{r.rows_after} "
            f"| `{r.hash_before}`→`{r.hash_after}` | `{r.constraints_before}` → `{r.constraints_after}` "
            f"| {r.verdict} |"
        )

    lines += ["", "## 3. 步骤意图与判定依据", ""]
    for r in results:
        if r.note:
            lines.append(f"- **{r.phase} {r.label}**：{r.note}")

    lines += ["", "## 4. 错误明细（仅非 OK 行，含完整首行报错）", ""]
    for r in results:
        if r.actual != "OK":
            lines.append(f"- **{r.phase} {r.label}** `{r.actual}` — {r.detail}")

    lines += ["", "## 5. 归属列可空性审计（分支侧全库）", ""]
    lines.append(f"共 {len(nullability)} 个归属列；`is_nullable` 全为 `NO` 时，复合外键的 MATCH SIMPLE 放行路径不可达。")
    lines.append("")
    lines.append("| 表 | 列 | is_nullable |")
    lines.append("| --- | --- | --- |")
    for row in nullability:
        lines.append(f"| `{row['tbl']}` | {row['column_name']} | {row['is_nullable']} |")
    nullable = [r for r in nullability if r["is_nullable"] == "YES"]
    lines.append("")
    lines.append(f"- 可空归属列数量：**{len(nullable)}**")

    lines += ["", "## 6. 对照：主支 organization_id 可空（已回滚）", ""]
    for key, value in contrast.items():
        lines.append(f"- {key}：`{value}`")

    lines += ["", "## 7. 最终状态", ""]
    lines.append(f"- 状态：`{final_constraints}`")
    lines.append(f"- 资产表最终行数：{final_rows}")

    out = Path(__file__).resolve().parent
    (out / "t3-dirty-state.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "t3-dirty-state.json").write_text(
        json.dumps(
            {
                "lab": LAB,
                "template": TEMPLATE,
                "port": PORT,
                "passed": passed,
                "total": len(results),
                "steps": [r.__dict__ for r in results],
                "scenarios": [
                    {"scenario": n, "steps": s, "fact": f} for n, s, f in SCENARIOS
                ],
                "ownership_nullability": nullability,
                "main_side_contrast": contrast,
                "final_constraints": final_constraints,
                "final_rows": final_rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nPASS {passed}/{len(results)} → {out / 't3-dirty-state.md'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
