"""D-10.3 要求 6 的 RLS 行为验收：业务主体不可见/不可写、审计主体可见/可写。

判据口径（v1.4.6 加硬的那一条）
------------------------------
**不得以「有无异常」为判据**。RLS 先于守卫生效，越权写入会静默退化成
``UPDATE 0`` 而不抛错（D-10.2.3 陷阱 b）。所以：

* 读取用例断言**返回行数**；
* 写入用例断言**影响行数**，并**回读**被改列确认值已变；
* 期望「被拒」的写入，若返回 0 行或抛错都算拒绝，但**必须记录是哪一种**——
  0 行（RLS 静默过滤）与 P0001（守卫拒绝）含义不同，混为一谈会掩盖缺陷。

三类主体
--------
| 主体 | 身份 | 预期 |
| --- | --- | --- |
| 业务 | ``svc_asset`` + 业务 GUC | 隔离项目与隔离行**不可见、不可写** |
| 审计 | ``afs_audit`` + ``app.include_quarantine='on'`` | **可见、可写**（D-10.3 要求 3/4） |
| 修复（已失效） | ``afs_data_repair`` + **已盖章工单** | 应**看不见**——例外随盖章失效 |

第三类是这套机制最容易被忽略的性质：映射行一旦 ``applied_at`` 非空，
可见性与可改性**同时**失效。不测它，就无法区分「例外按需生效」与「例外永久开着」。

用法::

    python rls_acceptance.py --port 15444
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import asyncpg

TENANT = "00000000-0000-0000-0000-000000000001"
ORG1 = "00000000-0000-0000-0000-000000000011"
SENTINEL_ROW = "av-sentinel"
QUARANTINE_ID = f"quarantine-{ORG1}"
TICKET = "WO-OWN-2026-1120-001"
ASSETS = "asset_service.asset_versions"
PROJECTS = "project_planning.projects"


@dataclass
class Case:
    cid: str
    desc: str
    ok: bool
    detail: str


def _rec(rows: list[Case], cid: str, desc: str, ok: bool, detail: str) -> None:
    rows.append(Case(cid, desc, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid:<4} {desc}  {detail}")


async def business_guc(conn: asyncpg.Connection) -> None:
    """业务主体的上下文：本租户 + 本项目集合全开。

    刻意用 ``'*'``：如果隔离可见性依赖「白名单恰好没列它」，
    那么 ``'*'`` 就会把它放出来——用最宽的设置来测，才能证明隔离是结构性的。
    """
    await conn.execute(f"SELECT set_config('app.current_tenant_id', '{TENANT}', true)")
    await conn.execute(f"SELECT set_config('app.current_org_id', '{ORG1}', true)")
    await conn.execute("SELECT set_config('app.current_projects', '*', true)")


async def audit_guc(conn: asyncpg.Connection) -> None:
    await business_guc(conn)
    await conn.execute("SELECT set_config('app.include_quarantine', 'on', true)")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="<ROTATED-2026-11-28>")
    ap.add_argument("--db", default="ai_film_industrial")
    args = ap.parse_args()

    cases: list[Case] = []
    conn = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.db,
    )

    async def scalar_as(role: str, guc, sql: str, *params):
        """在独立事务里以 role 身份执行一次查询后返回标量。

        每条用例单独一个事务是刻意的：RLS 拒绝写入返回 0 行不报错，
        但守卫拒绝或权限不足会抛错并使事务进入 aborted 状态——共用事务会让
        一条用例的失败污染后面的用例，把「测出来了」变成「全都没跑」。
        """
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            await guc(conn)
            return await conn.fetchval(sql, *params)

    async def exec_as(role: str, guc, sql: str, *params):
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            await guc(conn)
            return await conn.execute(sql, *params)

    try:
        # ── A. 业务主体：projects 域用 svc_project_planning，asset 域用 svc_asset ──
        n = await scalar_as(
            "svc_project_planning", business_guc,
            f"SELECT count(*) FROM {PROJECTS} WHERE entity_id = $1", QUARANTINE_ID,
        )
        _rec(cases, "A1", "业务主体查隔离项目 → 不可见（即使 current_projects='*'）",
             n == 0, f"返回 {n} 行（期望 0）")

        n = await scalar_as(
            "svc_asset", business_guc,
            f"SELECT count(*) FROM {ASSETS} WHERE entity_id = $1", SENTINEL_ROW,
        )
        _rec(cases, "A2", "业务主体查隔离区内的哨兵行 → 不可见", n == 0,
             f"返回 {n} 行（期望 0）")

        # 写：断言影响行数。0 行 = RLS 静默过滤，是「拒绝」而非「成功」。
        status = await exec_as(
            "svc_asset", business_guc,
            f"UPDATE {ASSETS} SET project_id = 'proj-alpha' WHERE entity_id = $1",
            SENTINEL_ROW,
        )
        affected = int(status.split()[-1])
        got = await scalar_as(
            "svc_asset", business_guc,
            "SELECT count(*) FROM asset_service.asset_versions "
            "WHERE entity_id = $1 AND project_id = 'proj-alpha'", SENTINEL_ROW,
        )
        _rec(cases, "A3", "业务主体改哨兵行归属 → 0 行且值未变（静默拒绝）",
             affected == 0 and got == 0,
             f"影响 {affected} 行；回读已改为 proj-alpha 的行数={got}")

        try:
            await exec_as(
                "svc_asset", business_guc,
                "INSERT INTO asset_service.asset_versions "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                "owner_module, tenant_id, organization_id, created_at, created_by, "
                "updated_at, project_id, asset_type, sha256, storage_ref, "
                "source_asset_version_ids) VALUES "
                "('av-intrude','AssetVersion',2,1,'GENERATED','asset_service',$1,$2,"
                "now(),'rls-test',now(),$3,'VIDEO',repeat('b',64),"
                "'{\"bucket\":\"b\",\"key\":\"av-intrude\"}'::jsonb,'[]'::jsonb)",
                TENANT, ORG1, QUARANTINE_ID,
            )
            inserted = await scalar_as(
                "postgres", business_guc,
                "SELECT count(*) FROM asset_service.asset_versions WHERE entity_id='av-intrude'",
            )
            _rec(cases, "A4", "业务主体向隔离项目新增行 → 被拒", inserted == 0,
                 f"无异常但写入 {inserted} 行")
        except asyncpg.PostgresError as e:
            _rec(cases, "A4", "业务主体向隔离项目新增行 → 被拒", True,
                 f"{type(e).__name__}: {str(e).splitlines()[0][:60]}")

        # ── B. 审计主体 ────────────────────────────────────────────────
        n = await scalar_as(
            "afs_audit", audit_guc,
            f"SELECT count(*) FROM {PROJECTS} WHERE entity_id = $1", QUARANTINE_ID,
        )
        _rec(cases, "B1", "审计主体（include_quarantine=on）查隔离项目 → 可见",
             n == 1, f"返回 {n} 行（期望 1）")

        n = await scalar_as(
            "afs_audit", audit_guc,
            f"SELECT count(*) FROM {ASSETS} WHERE entity_id = $1", SENTINEL_ROW,
        )
        _rec(cases, "B2", "审计主体查隔离区哨兵行 → 可见", n == 1, f"返回 {n} 行（期望 1）")

        status = await exec_as(
            "afs_audit", audit_guc,
            f"UPDATE {PROJECTS} SET project_name = project_name WHERE entity_id = $1",
            QUARANTINE_ID,
        )
        affected = int(status.split()[-1])
        _rec(cases, "B3", "审计主体改写隔离项目 → 影响 1 行（可写是真的可写）",
             affected == 1, f"影响 {affected} 行")

        try:
            status = await exec_as(
                "afs_audit", audit_guc,
                f"UPDATE {PROJECTS} SET status = 'ACTIVE' WHERE entity_id = $1",
                QUARANTINE_ID,
            )
            affected = int(status.split()[-1])
            now = await scalar_as(
                "postgres", audit_guc,
                f"SELECT status FROM {PROJECTS} WHERE entity_id = $1", QUARANTINE_ID,
            )
            _rec(cases, "B4", "把隔离项目改回 ACTIVE → 被拒（CHECK 拦）",
                 affected == 0 and now == "ARCHIVED",
                 f"影响 {affected} 行；回读 status={now}")
        except asyncpg.PostgresError as e:
            now = await scalar_as(
                "postgres", audit_guc,
                f"SELECT status FROM {PROJECTS} WHERE entity_id = $1", QUARANTINE_ID,
            )
            _rec(cases, "B4", "把隔离项目改回 ACTIVE → 被拒（CHECK 拦）",
                 now == "ARCHIVED",
                 f"{type(e).__name__}；回读 status={now}")

        # ── C. 已盖章工单：修复通道应失效 ──────────────────────────────
        async def repair_guc(c):
            await c.execute(f"SELECT set_config('app.ownership_repair_ticket', '{TICKET}', true)")

        n = await scalar_as(
            "afs_data_repair", repair_guc,
            f"SELECT count(*) FROM {ASSETS} WHERE entity_id = $1", SENTINEL_ROW,
        )
        stamped = await scalar_as(
            "postgres", repair_guc,
            "SELECT count(*) FROM project_planning.ownership_repair_map "
            "WHERE ticket_ref = $1 AND applied_at IS NOT NULL", TICKET,
        )
        _rec(cases, "C1", "工单已盖章后 repair 不再看见该行（例外随盖章失效）",
             n == 0 and stamped > 0,
             f"可见 {n} 行；已盖章映射 {stamped} 条")

        # ── D. 干净对照未被误伤 ───────────────────────────────────────
        n = await scalar_as(
            "svc_asset", business_guc,
            f"SELECT count(*) FROM {ASSETS} WHERE entity_id = 'av-clean'",
        )
        _rec(cases, "D1", "干净对照行对业务主体仍可见（未误伤）", n == 1,
             f"返回 {n} 行（期望 1）")

        passed = sum(1 for c in cases if c.ok)
        print("-" * 88)
        print(f"RLS 验收：{passed}/{len(cases)} 通过")
        return 0 if passed == len(cases) else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
