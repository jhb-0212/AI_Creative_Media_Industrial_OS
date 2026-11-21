"""在真实克隆库上验证补齐迁移：upgrade / VALIDATE / 新写入语义 / downgrade。

为什么要有这个脚本，而不是「看一眼 SQL 觉得对」：
上一轮 T3 的三个脚本缺陷（INSERT 少列、`"char"` 客户端比较、哨兵值没落位）都说明
**未执行的迁移脚本和注释没有区别**。本脚本把迁移里的 ``upgrade_steps()`` 原样取出、
在真 PostgreSQL 上跑，验证四件互不替代的事：

  D1  clean 库：14 条归属外键全部建成，且 ``convalidated = false``
  D2  clean 库：``VALIDATE`` 全部成功（证明空数据下可闭环，不是永远只能 NOT VALID）
  D3  dirty 库：存在 ``project_id`` 指向不存在项目的行时
        - upgrade 仍成功（NOT VALID 的真实语义）
        - **新写入**违规行被 23503 拒绝（NOT VALID ≠ 不生效）
        - ``VALIDATE`` 以 23503 失败（证明「先建约束后修数据」无法闭环）
  D4  downgrade 后无残留（约束、表全部消失，且不误删既有对象）

数据库全部是从 ``ai_film_industrial``（15444，分支链 head ``c4e5f6a7b8d9``）克隆的
一次性库，脚本结束即删除。**不触碰模板库、main 链、ai_film_verify、生产。**

用法::

    python data/migrations/pending/feat-ownership/verify_on_clone.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import asyncpg

HOST = "localhost"
PORT = 15444
USER = "postgres"
PASSWORD = "replay_super_pw"
TEMPLATE = "ai_film_industrial"
ADMIN_DB = "postgres"

LAB_CLEAN = "mig_lab_clean"
LAB_DIRTY = "mig_lab_dirty"
LAB_DOWN = "mig_lab_down"

T1 = "11111111-1111-1111-1111-111111111111"
O1 = "22222222-2222-2222-2222-222222222222"
P1 = "proj-1"

HERE = Path(__file__).resolve().parent


def load_migration():
    """按文件路径加载迁移模块（它不在 versions/ 里，也不是包）。"""
    path = next(HERE.glob("*_ownership_fk_backfill.py"))
    spec = importlib.util.spec_from_file_location("ownership_migration", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 必须先登记再执行：dataclass 在解析注解时会回查 sys.modules[cls.__module__]，
    # 未登记则 AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MIG = load_migration()

results: list[tuple[str, str, bool, str]] = []


def check(code: str, title: str, ok: bool, detail: str = "") -> None:
    results.append((code, title, ok, detail))


async def connect(db: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=db
    )


async def recreate(db: str) -> None:
    conn = await connect(ADMIN_DB)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}"')
        await conn.execute(f'CREATE DATABASE "{db}" TEMPLATE "{TEMPLATE}"')
    finally:
        await conn.close()


async def drop(db: str) -> None:
    conn = await connect(ADMIN_DB)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}"')
    finally:
        await conn.close()


async def run_steps(conn: asyncpg.Connection, steps) -> None:
    for step in steps:
        try:
            await conn.execute(step.sql)
        except Exception as exc:  # noqa: BLE001 - 需要原样上报，不吞
            raise RuntimeError(f"[{step.code}] {step.title} 失败: {exc}") from exc


async def fk_state(conn: asyncpg.Connection) -> dict[str, bool]:
    rows = await conn.fetch(
        """
        SELECT conname, convalidated
        FROM pg_constraint
        WHERE conname = ANY($1::text[])
        """,
        [name for _, name in MIG.OWNERSHIP_FKS],
    )
    return {r["conname"]: r["convalidated"] for r in rows}


async def table_exists(conn: asyncpg.Connection, qualified: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", qualified))


async def seed_dirty(conn: asyncpg.Connection) -> None:
    """造一行「归属指向不存在项目」的资产，模拟 legacy-unassigned 类存量。"""
    await conn.execute(
        f"""
        INSERT INTO project_planning.projects
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             created_at, created_by, updated_at, project_name, project_tier,
             tenant_id, organization_id)
        VALUES ('{P1}', 'Project', 2, 1, 'ACTIVE', 'project_planning',
                now(), 'verify', now(), 'proj-1', 'P1', '{T1}', '{O1}')
        """
    )
    await conn.execute(
        f"""
        INSERT INTO asset_service.asset_versions
            (entity_id, entity_type, schema_version, entity_version, status, owner_module,
             tenant_id, organization_id, project_id, created_at, created_by, updated_at,
             asset_type, sha256, storage_ref, source_asset_version_ids)
        VALUES ('av-ghost', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                '{T1}', '{O1}', 'ghost-project', now(), 'verify', now(),
                'VIDEO', repeat('a', 64),
                '{{"bucket":"b","key":"av-ghost"}}'::jsonb, '[]'::jsonb)
        """
    )


async def case_clean() -> None:
    await recreate(LAB_CLEAN)
    conn = await connect(LAB_CLEAN)
    try:
        await run_steps(conn, MIG.upgrade_steps())

        state = await fk_state(conn)
        check(
            "D1",
            f"14 条归属外键全部建成为 NOT VALID（实得 {len(state)}/14）",
            len(state) == len(MIG.OWNERSHIP_FKS) and not any(state.values()),
            f"convalidated 全为 false = {not any(state.values())}",
        )
        check(
            "D1b",
            "organizations 表与 3 个唯一键就位",
            await table_exists(conn, "project_planning.organizations")
            and await conn.fetchval(
                """SELECT count(*) FROM pg_constraint
                   WHERE conname IN ('uq_organizations_owner_tenant',
                                     'uq_projects_entity_tenant',
                                     'uq_projects_entity_tenant_org')"""
            )
            == 3,
        )

        validated, failed = [], []
        for qualified, name in MIG.OWNERSHIP_FKS:
            try:
                await conn.execute(f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {name}")
                validated.append(name)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{name}: {exc}")
        check(
            "D2",
            "空数据下 VALIDATE 全部成功（证明可闭环，不是永远只能 NOT VALID）",
            not failed,
            f"成功 {len(validated)}/{len(MIG.OWNERSHIP_FKS)}；失败 {failed}",
        )
        state = await fk_state(conn)
        check("D2b", "VALIDATE 后 convalidated 全为 true", all(state.values()))

        # 幂等性：整套语句再跑一遍不得报错、不得重复建对象
        await run_steps(conn, MIG.upgrade_steps())
        state2 = await fk_state(conn)
        check(
            "D2c",
            "重复执行 upgrade 幂等（无报错、约束数不变）",
            len(state2) == len(MIG.OWNERSHIP_FKS),
            f"第二次执行后约束数 {len(state2)}",
        )
    finally:
        await conn.close()


async def case_dirty() -> None:
    await recreate(LAB_DIRTY)
    conn = await connect(LAB_DIRTY)
    try:
        await seed_dirty(conn)
        await run_steps(conn, MIG.upgrade_steps())

        state = await fk_state(conn)
        check(
            "D3a",
            "脏行存在时 upgrade 仍成功（NOT VALID 不扫描历史行）",
            len(state) == len(MIG.OWNERSHIP_FKS) and not any(state.values()),
        )

        # NOT VALID ≠ 不生效：新写入同样违规必须被拒
        denied = None
        try:
            await conn.execute(
                f"""
                INSERT INTO asset_service.asset_versions
                    (entity_id, entity_type, schema_version, entity_version, status, owner_module,
                     tenant_id, organization_id, project_id, created_at, created_by, updated_at,
                     asset_type, sha256, storage_ref, source_asset_version_ids)
                VALUES ('av-ghost-2', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                        '{T1}', '{O1}', 'ghost-project-2', now(), 'verify', now(),
                        'VIDEO', repeat('b', 64),
                        '{{"bucket":"b","key":"av-ghost-2"}}'::jsonb, '[]'::jsonb)
                """
            )
        except asyncpg.exceptions.ForeignKeyViolationError as exc:
            denied = str(exc)
        check(
            "D3b",
            "新写入指向不存在项目 → 23503 被拒（NOT VALID 对新写入仍生效）",
            denied is not None,
            (denied or "未被拒绝！"),
        )

        valid_ok = True
        try:
            await conn.execute(
                f"""
                INSERT INTO asset_service.asset_versions
                    (entity_id, entity_type, schema_version, entity_version, status, owner_module,
                     tenant_id, organization_id, project_id, created_at, created_by, updated_at,
                     asset_type, sha256, storage_ref, source_asset_version_ids)
                VALUES ('av-ok', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                        '{T1}', '{O1}', '{P1}', now(), 'verify', now(),
                        'VIDEO', repeat('c', 64),
                        '{{"bucket":"b","key":"av-ok"}}'::jsonb, '[]'::jsonb)
                """
            )
        except Exception as exc:  # noqa: BLE001
            valid_ok = False
            denied = f"{type(exc).__name__}: {exc}"
        check("D3c", "归属正确的写入不被误伤", valid_ok, "" if valid_ok else str(denied))

        # 组织错配必须由三列外键拦下（两列外键看不见 organization_id）
        mismatch_denied = None
        try:
            await conn.execute(
                f"""
                INSERT INTO asset_service.asset_versions
                    (entity_id, entity_type, schema_version, entity_version, status, owner_module,
                     tenant_id, organization_id, project_id, created_at, created_by, updated_at,
                     asset_type, sha256, storage_ref, source_asset_version_ids)
                VALUES ('av-mismatch', 'AssetVersion', 2, 1, 'GENERATED', 'asset_service',
                        '{T1}', '99999999-9999-9999-9999-999999999999', '{P1}', now(), 'verify', now(),
                        'VIDEO', repeat('d', 64),
                        '{{"bucket":"b","key":"av-mismatch"}}'::jsonb, '[]'::jsonb)
                """
            )
        except asyncpg.exceptions.ForeignKeyViolationError as exc:
            mismatch_denied = str(exc)
        check(
            "D3d",
            "组织错配写入被 23503 拒绝（三列外键的真实收益）",
            mismatch_denied is not None,
            (mismatch_denied or "未被拒绝！"),
        )

        # 脏行阻塞 VALIDATE：这就是「先建约束后修数据」的死结
        validate_err = None
        try:
            await conn.execute(
                "ALTER TABLE asset_service.asset_versions "
                "VALIDATE CONSTRAINT fk_asset_versions_project_tenant_org"
            )
        except asyncpg.exceptions.ForeignKeyViolationError as exc:
            validate_err = str(exc).splitlines()[0]
        check(
            "D3e",
            "脏行未清时 VALIDATE 必然 23503（②前向新建不消除旧行，故无法闭环）",
            validate_err is not None,
            (validate_err or "竟然通过 —— 说明脏行没造对"),
        )
    finally:
        await conn.close()


async def case_downgrade() -> None:
    await recreate(LAB_DOWN)
    conn = await connect(LAB_DOWN)
    try:
        before = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype IN ('f','u')"
        )
        await run_steps(conn, MIG.upgrade_steps())
        mid = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype IN ('f','u')"
        )
        await run_steps(conn, MIG.downgrade_steps())

        after = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE contype IN ('f','u')"
        )
        check(
            "D4a",
            "downgrade 后约束数回到基线（无残留）",
            after == before,
            f"before={before} mid={mid} after={after}（mid 应多 {before and len(MIG.OWNERSHIP_FKS) + 3}）",
        )
        check(
            "D4b",
            "downgrade 后 organizations 表被移除",
            not await table_exists(conn, "project_planning.organizations"),
        )
        residual = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = ANY($1::text[])",
            [name for _, name in MIG.OWNERSHIP_FKS],
        )
        check("D4c", "无同名约束残留", residual == 0, f"残留 {residual}")
    finally:
        await conn.close()


async def main() -> int:
    try:
        await case_clean()
        await case_dirty()
        await case_downgrade()
    finally:
        for db in (LAB_CLEAN, LAB_DIRTY, LAB_DOWN):
            try:
                await drop(db)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 清理 {db} 失败: {exc}", file=sys.stderr)

    passed = sum(1 for r in results if r[2])
    total = len(results)
    width = max(len(r[1]) for r in results)
    print("=" * (width + 26))
    print(f"补齐迁移克隆库验证：{passed}/{total} 通过")
    print("=" * (width + 26))
    for code, title, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {code:<4} {title}")
        if detail:
            print(f"          └─ {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
