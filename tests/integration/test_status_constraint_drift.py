"""数据库 ``status`` CHECK 必须与 ORM 声明逐表一致。

为什么需要这条断言
------------------
``alembic check`` 走 autogenerate，而 SQLAlchemy 明确不比较 CHECK 约束。于是
「ORM 已把新状态写进枚举、迁移却还在用旧状态集」这类漂移对全部既有闸门不可见：
ruff / mypy / contract_check / freeze_manifest / alembic check / CI 全绿，而真实库
上写那条新状态直接 23514（check_violation）。

这不是假想。``a7b8c9d0e1f2`` 引入 ``ArtifactManifestStatus.ABANDONED``，ORM 三值、
库里两值，两条生产放弃路径（``generation_service`` 的 T1 放弃、
``workers/generation/executor`` 的恢复放弃）在真实库上必然失败。同形态的坑在
``generation_tasks`` 上已经出现过一次——``test_asset_registration_constraints`` 的
sample fixture 末尾那段「把在途任务复位，否则 CI 的 downgrade -1 过不去」的注释
就是它的痕迹。**修一次不够，需要判据。**

两个方向都查
------------
1. ORM 有、库里旧（少值）→ 新状态写不进去，业务路径在真实库上失败；
2. 库里有、ORM 没有（多约束）→ 要么迁移手写了 ORM 不认识的约束，要么
   ``packages.shared.orm_registry`` 漏导了某个域。后者更危险：它会让方向 1
   的地毯变成筛子——漏导的域根本不在比较集合里，从而静默通过。

为什么只比 ``status`` 而不比全部 CHECK
--------------------------------------
``status`` 约束由 ``status_check_sql`` 统一渲染成 ``status IN (...)'``，两侧形态
一致，可以逐值比对。其它 CHECK（例如 locator 正则与哈希前缀的三重约束）在 ORM
里是 Python 表达式、在库里是 PG 规范化后的表达式，逐字比较会全是假红灯。要管
那一类得另做语义级比对，不能靠把这里放宽成「模糊匹配」——那等于让断言失效。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import Base, build_migration_url
from packages.shared.orm_registry import load_all_orm_models

# 约束命名是仓库约定（见 status_constraint.status_check），不是对某个具体表的假设。
_STATUS_CONSTRAINT = re.compile(r"^ck_[a-z0-9_]+_status$")
# 只认大写枚举字面量：``'NO_LIFECYCLE'::text``、``'PREPARED'::character varying``
# 里的类型名都是小写，不会被误抓。
_QUOTED_VALUE = re.compile(r"'([A-Z][A-Z0-9_]*)'")

_DB_QUERY = sa.text(
    """
    SELECT n.nspname, cl.relname, con.conname,
           pg_get_expr(con.conbin, con.conrelid) AS expr
    FROM pg_constraint con
    JOIN pg_class cl ON cl.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE con.contype = 'c' AND con.conname ~ '^ck_.*_status$'
    ORDER BY 1, 2
    """
)


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """迁移角色（超级用户）连库：本用例只读系统目录，不需要租户上下文。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


def _values(raw: str) -> set[str]:
    return set(_QUOTED_VALUE.findall(raw))


def _declared_status_sets() -> dict[tuple[str, str], set[str]]:
    """ORM 声明的 (schema, table) → 允许状态集。"""
    load_all_orm_models()
    declared: dict[tuple[str, str], set[str]] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            name = constraint.name or ""
            if _STATUS_CONSTRAINT.match(name):
                key = (table.schema or "", table.name)
                declared[key] = _values(str(constraint.sqltext))
    return declared


async def test_status_constraints_match_orm_declaration(
    admin_engine: AsyncEngine,
) -> None:
    declared = _declared_status_sets()
    assert declared, "ORM 里一个 status 约束都没找到——meta 未加载，断言会假绿"

    async with admin_engine.connect() as conn:
        rows = (await conn.execute(_DB_QUERY)).all()

    stored: dict[tuple[str, str], tuple[str, set[str]]] = {}
    for schema, table, name, expr in rows:
        stored[(schema, table)] = (name, _values(expr or ""))

    missing_in_db = sorted(set(declared) - set(stored))
    assert not missing_in_db, (
        "ORM 声明了 status 约束，数据库里却没有同名约束：\n"
        + "\n".join(f"  - {s}.{t}" for s, t in missing_in_db)
        + "\n（迁移漏建，或约束被改名）"
    )

    missing_in_orm = sorted(set(stored) - set(declared))
    assert not missing_in_orm, (
        "数据库里有 status 约束，ORM 元数据里却没有对应表/约束：\n"
        + "\n".join(f"  - {s}.{t}（{stored[(s, t)][0]}）" for s, t in missing_in_orm)
        + "\n两种可能：① 迁移手写了 ORM 不认识的约束；② "
        "packages.shared.orm_registry 漏导了该域——后者会让本用例对该域静默失明。"
    )

    mismatched = [
        (key, sorted(expected), sorted(stored[key][1]))
        for key, expected in declared.items()
        if expected != stored[key][1]
    ]
    assert not mismatched, (
        "status 允许集合不一致（ORM 声明 vs 数据库实际）：\n"
        + "\n".join(
            f"  - {schema}.{table}\n      ORM: {orm}\n      DB : {db}"
            for (schema, table), orm, db in mismatched
        )
        + "\n库里缺的那几个值写不进去（23514），而 alembic check 看不见这类差异。"
    )
