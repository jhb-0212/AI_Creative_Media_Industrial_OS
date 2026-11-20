"""逐行映射与存量归属订正（受控例外通道）。

这个脚本做三件事，顺序不可换：

1. **探测**：把每张表里「归属不成立」的行找出来（左连接目标表比对）；
2. **判定**：按工单里写死的 R1~R3 规则推导目标归属，推不出的标 ``MANUAL``；
3. **订正**：写映射行 → ``SET LOCAL ROLE afs_data_repair`` → 设令牌 GUC → UPDATE
   → 断言影响行数 = 1 且回读值已变 → 给映射行盖 ``applied_at``。

为什么必须走受控例外而不是直接 UPDATE
------------------------------------
13 张表有不可变守卫，``project_id``/``organization_id`` 本身就是不可变列
（这正是 D-10.2 指出的 I-2 不变量：已确认归属不得被改写）。直接 UPDATE 会
``P0001``。例外通道不是「绕过守卫」，而是「守卫在四重条件下放行归属三列」：
专用角色 + 工单令牌 + 映射行未盖章 + 变更列 ⊆ 归属三列。四个条件缺一不可。

为什么断言要「影响行数 + 回读」而不是「没报错」
--------------------------------------------
ADR-R40 D-10.2.3 陷阱（b）：RLS 先于守卫生效，越权写会静默退化成 ``UPDATE 0``，
**不抛异常**。只看「有没有报错」会把「一行没改」读成「改成功了」。

用法::

    python repair_ownership.py --port 15444 --plan      # 只探测与判定，不写
    python repair_ownership.py --port 15444             # 执行订正
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

import asyncpg

TICKET = "WO-OWN-2026-1120-001"
REPAIR_ROLE = "afs_data_repair"
GUC = "app.ownership_repair_ticket"

PROJECTS = "project_planning.projects"
ORGS = "project_planning.organizations"
MAP = "project_planning.ownership_repair_map"
IS_Q = "project_planning.is_quarantine_project"

#: (schema.table, 主键列, 是否有 project_id)
#: 覆盖范围**大于** 14 条外键：``qc_reports`` 在这里但仍无归属外键，
#: 所以它会被订正却不会出现在台账里（见工单「与台账的关系」）。
SCOPE: tuple[tuple[str, str, bool], ...] = (
    ("asset_service.asset_versions", "entity_id", True),
    ("asset_service.delivery_manifests", "entity_id", True),
    ("asset_service.qc_reports", "qc_report_id", True),
    ("generation_execution.generation_tasks", "entity_id", True),
    ("generation_execution.shot_route_selections", "entity_id", True),
    ("generation_execution.production_routes", "entity_id", False),
    ("qc_governance.gate_decisions", "entity_id", True),
    ("qc_governance.qc_review_sessions", "entity_id", True),
    ("shot_planning.shot_drafts", "entity_id", True),
    ("shot_planning.shot_revisions", "entity_id", True),
    ("shot_planning.shot_specs", "entity_id", True),
    ("shot_planning.shot_visual_plans", "entity_id", True),
    ("shot_planning.constraint_packs", "entity_id", True),
    ("workflow.approval_flows", "entity_id", True),
    ("workflow.approval_steps", "entity_id", True),
    ("workflow.approval_tickets", "entity_id", True),
)


@dataclass
class Finding:
    table: str
    key_col: str
    row_key: str
    old_project: str | None
    old_org: str
    old_tenant: str
    rule: str = ""
    new_project: str | None = None
    new_org: str | None = None
    new_tenant: str | None = None
    reason: str = ""
    manual: str = ""
    applied: str = ""

    @property
    def changed(self) -> dict[str, tuple[str | None, str | None]]:
        """只返回**确实变化**的归属列 → (旧值, 新值)。"""
        out: dict[str, tuple[str | None, str | None]] = {}
        if self.old_project != self.new_project:
            out["project_id"] = (self.old_project, self.new_project)
        if self.old_org != self.new_org:
            out["organization_id"] = (self.old_org, self.new_org)
        if self.old_tenant != self.new_tenant:
            out["tenant_id"] = (self.old_tenant, self.new_tenant)
        return out


async def discover(conn: asyncpg.Connection) -> list[Finding]:
    """找出所有归属不成立的行。探测用超级用户会话，故不受 RLS 影响。"""
    findings: list[Finding] = []

    for table, key_col, has_project in SCOPE:
        if has_project:
            rows = await conn.fetch(
                f"""
                SELECT t.{key_col}::text AS k,
                       t.project_id::text AS project_id,
                       t.organization_id::text AS org,
                       t.tenant_id::text AS tenant,
                       p.entity_id IS NOT NULL AS project_exists,
                       p.organization_id::text AS real_org,
                       p.tenant_id::text AS real_tenant
                  FROM {table} t
                  LEFT JOIN {PROJECTS} p ON p.entity_id = t.project_id
                 WHERE p.entity_id IS NULL
                    OR p.organization_id IS DISTINCT FROM t.organization_id
                    OR p.tenant_id IS DISTINCT FROM t.tenant_id
                 ORDER BY 1
                """
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT t.{key_col}::text AS k,
                       NULL::text AS project_id,
                       t.organization_id::text AS org,
                       t.tenant_id::text AS tenant,
                       false AS project_exists,
                       NULL::text AS real_org,
                       NULL::text AS real_tenant
                  FROM {table} t
                  LEFT JOIN {ORGS} o
                    ON o.organization_id = t.organization_id
                   AND o.tenant_id = t.tenant_id
                 WHERE o.organization_id IS NULL
                 ORDER BY 1
                """
            )

        for r in rows:
            f = Finding(
                table=table, key_col=key_col, row_key=r["k"],
                old_project=r["project_id"], old_org=r["org"], old_tenant=r["tenant"],
            )
            # R1：项目真实存在但组织/租户列与项目不一致 → 以项目为准
            if r["project_exists"] and (
                r["real_org"] != r["org"] or r["real_tenant"] != r["tenant"]
            ):
                f.rule = "R1"
                f.new_project = r["project_id"]
                f.new_org = r["real_org"]
                f.new_tenant = r["real_tenant"]
                f.reason = (
                    "组织/租户列与所属项目的真实归属不一致；项目是更强的归属事实源，"
                    "以项目真实 organization_id/tenant_id 为准"
                )
            elif has_project:
                # R2：项目不存在 → 移入该组织对应的隔离项目
                q = await conn.fetchrow(
                    f"""
                    SELECT entity_id::text AS qid
                      FROM {PROJECTS}
                     WHERE {IS_Q}(entity_id)
                       AND organization_id = $1::uuid
                       AND tenant_id = $2::uuid
                    """,
                    r["org"], r["tenant"],
                )
                if q is None:
                    f.manual = (
                        f"R2 需要组织 {r['org']} 的隔离项目，但它不存在"
                        "（该组织可能本身不在 organizations 中）"
                    )
                else:
                    f.rule = "R2"
                    f.new_project = q["qid"]
                    f.new_org = r["org"]
                    f.new_tenant = r["tenant"]
                    f.reason = (
                        "project_id 指向不存在的项目，无法判定真实业务归属；"
                        "按 D-10.3 移入该组织的隔离项目，不虚构真实归属"
                    )
            else:
                # R3：组织级孤儿 → 仅在候选唯一时订正
                cands = await conn.fetch(
                    f"SELECT organization_id::text AS o, tenant_id::text AS t "
                    f"FROM {ORGS} ORDER BY 1"
                )
                if len(cands) == 1:
                    f.rule = "R3"
                    f.new_project = None
                    f.new_org = cands[0]["o"]
                    f.new_tenant = cands[0]["t"]
                    f.reason = (
                        "组织级表且 (organization_id, tenant_id) 不在 organizations 中；"
                        "全库唯一真实组织为其归属，按唯一候选订正"
                    )
                else:
                    f.manual = (
                        f"R3 存在 {len(cands)} 个候选组织，无法唯一确定；转人工裁决"
                    )
            findings.append(f)
    return findings


def ownership_clause(f: Finding) -> tuple[str, list[object]]:
    """拼出 UPDATE 的 SET 与 WHERE。只碰归属三列，绝不夹带内容列。"""
    sets, params = [], []
    changed = f.changed
    i = 1
    for col in ("project_id", "organization_id", "tenant_id"):
        if col in changed:
            sets.append(f"{col} = ${i}")
            params.append(changed[col][1])
            i += 1
    where = f"{f.key_col}::text = ${i}"
    params.append(f.row_key)
    return f"SET {', '.join(sets)} WHERE {where}", params


async def insert_map(conn: asyncpg.Connection, f: Finding) -> None:
    """写映射行。**必须以迁移主体身份**执行——repair 角色对映射表只有 SELECT
    （R4：不授 INSERT，避免「谁都能给自己开例外」）。"""
    await conn.execute(
        f"""
        INSERT INTO {MAP} (table_name, row_key,
                           old_project_id, old_organization_id, old_tenant_id,
                           new_project_id, new_organization_id, new_tenant_id,
                           reason, ticket_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        f.table, f.row_key, f.old_project, f.old_org, f.old_tenant,
        f.new_project, f.new_org, f.new_tenant, f.reason, TICKET,
    )


async def update_ownership(conn: asyncpg.Connection, f: Finding) -> str:
    """以 ``afs_data_repair`` 身份走受控例外改归属列，并断言 + 回读。

    调用前必须已 ``SET LOCAL ROLE`` 与设置令牌 GUC。
    """
    clause, params = ownership_clause(f)
    status = await conn.execute(f"UPDATE {f.table} {clause}", *params)
    affected = int(status.split()[-1]) if status.startswith("UPDATE") else -1
    if affected != 1:
        raise RuntimeError(
            f"{f.table}/{f.row_key}: 期望影响 1 行，实际 {affected} 行"
            f"（0 行通常意味着 RLS 静默过滤，而非「无需修改」）"
        )

    # 回读：断言被改列**确实**是新值。
    # 用 to_jsonb 取列而不是直接 SELECT 列名——production_routes 没有 project_id 列，
    # 按列名写会 UndefinedColumnError，而这不是"该表没有归属列"该有的表现。
    got = await conn.fetchrow(
        f"SELECT to_jsonb(x) ->> 'project_id' AS project_id, "
        f"to_jsonb(x) ->> 'organization_id' AS org, "
        f"to_jsonb(x) ->> 'tenant_id' AS tenant "
        f"FROM {f.table} x WHERE x.{f.key_col}::text = $1",
        f.row_key,
    )
    checks = {
        "project_id": (got["project_id"], f.new_project),
        "organization_id": (got["org"], f.new_org),
        "tenant_id": (got["tenant"], f.new_tenant),
    }
    for col, (actual, want) in checks.items():
        if actual != want:
            raise RuntimeError(
                f"{f.table}/{f.row_key}: 回读 {col}={actual!r} 与目标 {want!r} 不一致"
            )
    return f"UPDATE 1 · 回读一致 · 变更列={sorted(f.changed)}"


async def stamp_map(conn: asyncpg.Connection, f: Finding) -> None:
    """给映射行盖 applied_at。同样须迁移主体身份（repair 无 map 表 UPDATE 权限）。"""
    await conn.execute(
        f"UPDATE {MAP} SET applied_at = now() "
        f"WHERE table_name = $1 AND row_key = $2 AND ticket_ref = $3",
        f.table, f.row_key, TICKET,
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="replay_super_pw")
    ap.add_argument("--db", default="ai_film_industrial")
    ap.add_argument("--plan", action="store_true", help="只探测与判定，不写库")
    args = ap.parse_args()

    conn = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.db,
    )
    try:
        findings = await discover(conn)
        todo = [f for f in findings if not f.manual]
        manual = [f for f in findings if f.manual]

        print(f"探测到归属不成立的行：{len(findings)} 行"
              f"（可自动判定 {len(todo)}，需人工 {len(manual)}）")
        print("-" * 96)
        for f in findings:
            if f.manual:
                print(f"  [MANUAL] {f.table:<44} {f.row_key:<16} {f.manual}")
                continue
            ch = ", ".join(f"{k}:{v[0]}→{v[1]}" for k, v in f.changed.items())
            print(f"  [{f.rule}] {f.table:<44} {f.row_key:<16} {ch}")

        if not findings:
            print("  （无违规行：所有行归属均已成立）")
        print("-" * 96)

        if args.plan:
            print("--plan 模式：未写入任何数据。")
            return 0
        if manual:
            print(f"拒绝执行：有 {len(manual)} 行需人工裁决。"
                  "fail-closed，不为凑闭环而猜测目标。")
            return 3
        if not todo:
            print("无事可做。")
            return 0

        # 整个订正在一个事务里：任一行断言失败，全部回滚，
        # 不留下「一部分已改、映射表已盖章」的半成品状态。
        #
        # 阶段划分是必须的，不是风格：
        #   1) 写映射行 —— repair 对 map 无 INSERT 权限，须迁移主体；
        #   2) 改归属列 —— 必须 repair 身份，否则守卫 P0001；
        #   3) 盖 applied_at —— 又要回到迁移主体。
        # 三段同事务，靠 SET LOCAL ROLE / RESET ROLE 切换身份。
        async with conn.transaction():
            for f in todo:
                await insert_map(conn, f)

            await conn.execute(f"SET LOCAL ROLE {REPAIR_ROLE}")
            await conn.execute(f"SELECT set_config('{GUC}', $1, true)", TICKET)
            for f in todo:
                f.applied = await update_ownership(conn, f)
                print(f"  [OK] {f.table:<44} {f.row_key:<16} {f.applied}")
            await conn.execute("RESET ROLE")

            for f in todo:
                await stamp_map(conn, f)
        print("-" * 96)
        print(f"订正完成：{len(todo)} 行，全部断言通过（影响行数=1 且回读一致）。")
        print(f"映射记录：{MAP} 中 ticket_ref = '{TICKET}'")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
