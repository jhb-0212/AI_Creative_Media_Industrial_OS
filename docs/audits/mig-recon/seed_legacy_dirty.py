"""在**外键建立之前**造出存量脏数据（模拟真实生产升级前的历史状态）。

为什么必须在外键之前
--------------------
``a9b8`` 建的是 ``NOT VALID`` 外键。``NOT VALID`` 只跳过**历史行扫描**，
对**新写入照样检查**。所以外键一旦建立，脏行就插不进去了——
这不是缺陷，正是「写入保护先行」的应有表现。

因此要验证「存量订正」这条链路，只能在**升级前**把脏行造出来，
即：``downgrade`` 到 ``c4e5f6a7b8d9`` → 造脏 → 再升级三层。
这也是真实生产升级的时序（历史数据先存在，约束后加）。

刻意造出的四类脏态
------------------
| 场景 | 表 | 脏点 | 三列外键会怎么判 |
| --- | --- | --- | --- |
| 哨兵 | ``asset_versions`` / ``qc_reports`` | ``project_id='legacy-unassigned'`` | 23503（项目不存在） |
| 幽灵 | ``gate_decisions`` | ``project_id='ghost-project'`` | 23503（项目不存在） |
| 组织错配 | ``generation_tasks`` | 项目属 org-1，行写 org-2 | 23503（三列不匹配） |
| 组织级孤儿 | ``production_routes`` | ``organization_id=org-2`` 且组织不存在 | 23503（两列外键） |

另造一行**干净对照**（``av-clean``），确保订正不会误伤正确数据。

口径
----
* 这是**合成数据**，用于验证修复链路可用；不是生产数据，不代表真实业务量。
* 行内容复用 ``guard_fixtures`` 的声明式模板（不手抄 DDL 副本）。
* 跨表引用显式处理：``qc_reports.asset_version_id`` 与 ``generation_tasks.route_id``
  都指向真实父行，否则夹具会撞既存外键 23503，导致整批用例静默失效——
  这个坑在上一轮挖过，这里直接消掉。

用法::

    python seed_legacy_dirty.py --port 15444 [--password ...]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard_fixtures as fx

TENANT = "00000000-0000-0000-0000-000000000001"
ORG1 = "00000000-0000-0000-0000-000000000011"
ORG2 = "00000000-0000-0000-0000-000000000012"
SENTINEL = "legacy-unassigned"
GHOST = "ghost-project"
PROJ_A = "proj-alpha"
PROJ_B = "proj-beta"

PROJECT_COLS = (
    "entity_id, entity_type, schema_version, entity_version, status, owner_module, "
    "created_at, created_by, updated_at, audit_ref, project_name, project_tier, "
    "tenant_id, organization_id"
)


def _project(entity_id: str, name: str) -> str:
    return (
        f"INSERT INTO project_planning.projects ({PROJECT_COLS}) VALUES ("
        f"'{entity_id}', 'Project', 2, 1, 'ACTIVE', 'project_planning', "
        f"now(), '(system) legacy-seed', now(), NULL, '{name}', 'B', "
        f"'{TENANT}', '{ORG1}')"
    )


def statements() -> list[tuple[str, str, bool]]:
    """返回 ``(标签, SQL, 是否模拟历史行)`` 顺序列表。

    第三位为 ``True`` 的语句在 ``session_replication_role = replica`` 下执行：
    它模拟的是「**在归属已分配断言建立之前**就已存在的历史行」。

    为什么需要这一步（而不是把哨兵写成普通 INSERT）
    ------------------------------------------------
    库里另有一层断言 ``assert_<table>_project_assigned``（**只覆盖
    ``asset_versions`` 与 ``qc_reports`` 两张表**），它在 INSERT 时直接拒绝
    ``legacy-unassigned``。这层断言是后加的，晚于真实存量数据，
    所以「历史哨兵行」在今天的写入路径上已不可能产生——这正是保护生效的证据，
    也是造数必须绕过它的原因。

    注意这不是绕过 13 个不可变守卫（那些只拦 UPDATE/DELETE，本就不拦 INSERT），
    也不进入任何产品路径：仅在造数脚本的事务内、仅作用于哨兵那两行。
    """
    return [
        # ── 真实项目（订正目标的合法归属）──────────────────────────────
        ("project:proj-alpha", _project(PROJ_A, "合成项目 Alpha"), False),
        ("project:proj-beta", _project(PROJ_B, "合成项目 Beta"), False),
        # ── 干净对照行（必须始终不被误伤）──────────────────────────────
        (
            "clean:asset_versions",
            fx.insert_sql(
                "asset_service.asset_versions", "av-clean",
                project=PROJ_A, tenant=TENANT, org=ORG1,
            ),
            False,
        ),
        # ── 前置父行：qc_reports 与 generation_tasks 都引用它们 ─────────
        (
            "ref:asset_versions",
            fx.insert_sql(
                "asset_service.asset_versions", "av-ref-qc-1",
                project=PROJ_A, tenant=TENANT, org=ORG1,
            ),
            False,
        ),
        (
            "ref:production_routes",
            fx.insert_sql(
                "generation_execution.production_routes", "rt-coverage-base",
                tenant=TENANT, org=ORG1,
            ),
            False,
        ),
        # ── 脏态 1：哨兵行（历史行，需绕过「归属已分配」断言）────────────
        (
            "dirty:sentinel:asset_versions",
            fx.insert_sql(
                "asset_service.asset_versions", "av-sentinel",
                project=SENTINEL, tenant=TENANT, org=ORG1,
            ),
            True,
        ),
        (
            "dirty:sentinel:qc_reports",
            fx.insert_sql(
                "asset_service.qc_reports", "qc-1",
                project=SENTINEL, tenant=TENANT, org=ORG1,
            ),
            True,
        ),
        # ── 脏态 2：幽灵项目（断言不拦它，因为它不是哨兵字面量）──────────
        (
            "dirty:ghost:gate_decisions",
            fx.insert_sql(
                "qc_governance.gate_decisions", "gd-ghost",
                project=GHOST, tenant=TENANT, org=ORG1,
            ),
            False,
        ),
        # ── 脏态 3：组织错配（项目属 org-1，行写 org-2）─────────────────
        (
            "dirty:org_mismatch:generation_tasks",
            fx.insert_sql(
                "generation_execution.generation_tasks", "gt-mismatch",
                project=PROJ_A, tenant=TENANT, org=ORG2,
            ),
            False,
        ),
        # ── 脏态 4：组织级孤儿（无 project_id，两列外键的目标）──────────
        (
            "dirty:orphan:production_routes",
            fx.insert_sql(
                "generation_execution.production_routes", "rt-orphan",
                tenant=TENANT, org=ORG2,
            ),
            False,
        ),
    ]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15444)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="<ROTATED-2026-11-28>")
    ap.add_argument("--db", default="ai_film_industrial")
    args = ap.parse_args()

    conn = await asyncpg.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.db,
    )
    try:
        # 前置：确认此刻**没有**归属外键（否则说明还没降级，脏行插不进去）
        n = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype='f' "
            "AND (conname LIKE 'fk_%project_tenant_org' OR conname LIKE 'fk_%organization_tenant')"
        )
        if n:
            print(f"拒绝执行：仍有 {n} 条归属外键。请先 downgrade 到 c4e5f6a7b8d9。")
            return 2

        async with conn.transaction():
            for label, sql, replica in statements():
                if replica:
                    # SET LOCAL 只在本事务内生效，事务结束自动恢复 origin。
                    # 只用于「历史行」两条，范围可控且退出即还原。
                    await conn.execute("SET LOCAL session_replication_role = replica")
                    await conn.execute(sql)
                    await conn.execute("SET LOCAL session_replication_role = origin")
                else:
                    await conn.execute(sql)
                print(f"  [ok] {label}{'  (模拟历史行)' if replica else ''}")

        print("\n存量快照（升级前）")
        print("-" * 72)
        # (表, 键列, 是否取 project_id)。projects 自身没有 project_id 列——
        # 它的归属列就是 entity_id + organization_id。
        snapshot = [
            ("project_planning.projects", "entity_id", False),
            ("asset_service.asset_versions", "entity_id", True),
            ("asset_service.qc_reports", "qc_report_id", True),
            ("qc_governance.gate_decisions", "entity_id", True),
            ("generation_execution.generation_tasks", "entity_id", True),
            ("generation_execution.production_routes", "entity_id", False),
        ]
        for tbl, key, has_project in snapshot:
            proj = "project_id::text" if has_project else "NULL::text"
            rows = await conn.fetch(
                f"SELECT {key}::text AS k, {proj} AS project_id, "
                f"organization_id::text AS organization_id FROM {tbl} ORDER BY 1"
            )
            print(f"  {tbl}（{len(rows)} 行）")
            for r in rows:
                flag = "  <- 脏" if r["project_id"] in (SENTINEL, GHOST) else ""
                print(
                    f"    {r['k']:<18} project={r['project_id']!s:<20} "
                    f"org={r['organization_id'][:8]}…{flag}"
                )
        print("-" * 72)
        print("造数完成：脏行已存在，且此刻**没有任何归属外键**在保护它们。")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
