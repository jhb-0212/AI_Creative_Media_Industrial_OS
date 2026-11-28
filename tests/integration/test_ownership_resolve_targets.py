"""归属解析 SQL 的目标必须真实存在（P1-06 的根因闸门）。

为什么需要这条闸门
------------------
``packages/identity/ownership.py`` 的 ``_RESOLVE_SQL`` 把「资源 → 归属」写成了
一段段手写 SQL，表名是**字符串**。R16 分域迁移把四张表搬到了各自的域 schema
（``production_planning`` / ``budget_quota`` / ``qc_governance``），而这段 SQL
没有跟着改。后果不是报错，而是更糟的形态：

- 服务层单测全绿（单测不经授权依赖，直接调服务）；
- 真实请求在授权依赖里抛 ``42P01 relation does not exist`` → 500；
- 于是「审批端点根本不可用」这件事，在测试与人工走查里都看不见。

字符串表名不会自己举手。所以判据必须**从库里实读**：把每条 SQL 里 ``FROM`` /
``JOIN`` 引用的表名抽出来，逐个 ``to_regclass`` 判存在。表搬走、改名、删表都会
当场变红，而不是等下一次有人手工点那个端点。

为什么不写一份「资源类型 → 表名」的清单来比：清单本身就是第二份事实源，
它和 SQL 会各自漂移。这里只解析 SQL 自己写的目标，不做任何映射。
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.identity.ownership import _RESOLVE_SQL
from packages.shared.database import build_migration_url

# ``FROM a.b`` / ``JOIN a.b``（含 ``AS`` 别名），只取两级限定名。
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", re.IGNORECASE)


def _table_refs(sql: str) -> list[str]:
    return sorted({match.group(1).lower() for match in _TABLE_REF.finditer(sql)})


def test_every_resolve_sql_has_at_least_one_table_ref() -> None:
    """解析器自身的防呆：抽不到表名说明 SQL 变形了，闸门会变成空转。"""
    empty = [rtype for rtype, sql in _RESOLVE_SQL.items() if not _table_refs(sql)]
    assert not empty, f"以下归属解析 SQL 抽不出表名（解析器或 SQL 需要修正）: {empty}"


async def test_resolve_sql_targets_exist_in_database() -> None:
    """每条归属解析 SQL 引用的表都必须能在库里解析到。"""
    engine = create_async_engine(build_migration_url())
    try:
        async with engine.connect() as conn:
            missing: list[str] = []
            for resource_type, sql in _RESOLVE_SQL.items():
                for qualified in _table_refs(sql):
                    exists = await conn.scalar(
                        text("SELECT to_regclass(:name) IS NOT NULL"),
                        {"name": qualified},
                    )
                    if not exists:
                        missing.append(f"{resource_type} → {qualified}")
            assert not missing, "归属解析 SQL 指向了不存在的表（分域迁移后未同步？）：" + "; ".join(
                missing
            )
    finally:
        await engine.dispose()


async def test_resolve_actually_executes_for_every_resource_type() -> None:
    """不只判表存在，还要真跑一遍：能执行、且查不到时返回 ``None``。

    为什么这条不可省：「表存在」不等于「列名对」。分域后若某张表的
    ``project_id`` / ``tenant_id`` 改名，``to_regclass`` 依然通过，而请求依然 500。
    """
    engine = create_async_engine(build_migration_url())
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, false)"),
                {"t": str(uuid.uuid4())},
            )
            for resource_type, sql in _RESOLVE_SQL.items():
                # Organization 的主键是 uuid，其余是字符串；给一个绝不存在的值。
                probe = (
                    str(uuid.uuid4())
                    if resource_type == "Organization"
                    else f"probe-{uuid.uuid4().hex}"
                )
                row = (await conn.execute(text(sql), {"rid": probe})).first()
                assert row is None, f"{resource_type} 的探针值不该解析出归属"
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
