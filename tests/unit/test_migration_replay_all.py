"""整条迁移链的「可回放」契约：upgrade / downgrade 都必须能被闸门的假 op 加载。

为什么单独立一条
----------------
闸门不连库，它把 ``op`` 换成一个只收集 SQL 的替身来回放迁移。这个替身的能力面
是**手写的**：迁移里凡出现它没实现的调用（``get_bind().execute(...).scalar_one()``
是最新踩到的一次），回放就崩在 AttributeError，而闸门会把崩溃报成「无法加载迁移」，
把排查方向指到文件路径上。

所以这里对**每一份**迁移都做一次双向回放：崩了就是契约破了，而不是某一条断言
碰巧没跑到。这条测试的价值不在断言数量，而在覆盖面——新增迁移自动进入覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import boundary_assertions as ba

_VERSIONS = Path(__file__).resolve().parents[2] / "data" / "migrations" / "versions"
_MIGRATIONS = sorted(p.name for p in _VERSIONS.glob("*.py") if p.name != "__init__.py")


def test_migrations_are_discovered() -> None:
    """发现集不能为空：空集会把这整条测试变成永远通过。"""
    assert len(_MIGRATIONS) >= 30


@pytest.mark.parametrize("filename", _MIGRATIONS)
def test_upgrade_is_replayable(filename: str) -> None:
    """升级回放不得抛错。

    刻意**不**要求语句非空：结构类 DDL（``op.create_table`` 等）在闸门的假 op 上
    是 no-op（它们不在本断言的价值范围内），所以「0 条语句」是合法结果。
    真正要抓的失败形态是崩溃——见 ``test_op_execute_heavy_migration_yields_sql``。
    """
    ba._rendered_migration_statements(f"data/migrations/versions/{filename}", direction="upgrade")


def test_op_execute_heavy_migration_yields_sql() -> None:
    """带 DO 块/守卫的迁移必须真的产出 SQL，证明回放不是空转。"""
    statements = ba._rendered_migration_statements(
        "data/migrations/versions/d0e1f2a3b4c5_storage_ref_controlled_locator.py"
    )
    assert statements
    assert any("ck_asset_versions_storage_ref_locator" in s for s in statements)


@pytest.mark.parametrize("filename", _MIGRATIONS)
def test_downgrade_is_replayable(filename: str) -> None:
    """降级回放不要求非空（f2b3 刻意什么都不做），但必须**不崩**。"""
    ba._rendered_migration_statements(f"data/migrations/versions/{filename}", direction="downgrade")
