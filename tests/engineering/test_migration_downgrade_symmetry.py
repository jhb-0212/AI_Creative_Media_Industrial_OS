"""迁移升级/降级的对称性断言（针对「授予有、撤销无」这类单边漏检）。

来历：``c5d6e7f8a9b0`` 首版在 ``upgrade()`` 里用 DO 块遍历现存 outbox 分区授予
运维角色权限，``downgrade()`` 却只撤了父表。PG 的分区 ACL 是独立于父表的条目，
``REVOKE ALL ON outbox.outbox_events`` 撤不掉 ``outbox_events_y202611`` 上的授权，
于是 ``DROP ROLE`` 停在 "role cannot be dropped because some objects depend on it"。

为什么不是「CI 已经能抓」就够了：``Migration downgrade roundtrip`` 需要真库、只在
CI 跑，本地迭代时几分钟才反馈一次；而且它报的是「DROP ROLE 失败」，不是「谁少撤了
一步」。这条断言把判据前移到源码层：**授予侧引用分区 DO 块，撤销侧必须引用对偶**，
毫秒级失败并直接指出文件。

判据不是「downgrade 里有字符串 revoke」——那种写法会漏掉顺序（先 DROP ROLE 再
REVOKE 在源码里看着也对称）。这里只判「对偶存在」，执行顺序由真库往返保证。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from packages.shared.partition_guard import (
    ops_partition_grant_do_block,
    ops_partition_revoke_do_block,
)

_MIGRATIONS = Path(__file__).resolve().parents[2] / "data" / "migrations" / "versions"

# 「授予 / 撤销」成对登记：动态分区权限的每一种授予方式都必须有对偶撤销。
# 新增一对生成器时登记到这里，否则判据对新授权面天然免疫。
_PARTITION_GRANT_PAIRS: tuple[tuple[str, str], ...] = (
    ("ops_partition_grant_do_block", "ops_partition_revoke_do_block"),
    (
        "grant_outbox_partition_statements",
        "revoke_outbox_partition_statements",
    ),
)


def _called_names(node: ast.AST) -> set[str]:
    """某个函数体里出现的被调用名（含 ``op.execute(...)`` 内层调用）。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _function_body(tree: ast.Module, name: str) -> ast.AST | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _asymmetric_migrations() -> list[str]:
    """升级侧授予分区权限、却不在 downgrade 里对偶撤销的迁移（含缺哪个函数）。"""
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        upgrade = _function_body(tree, "upgrade")
        if upgrade is None:
            continue
        granted = _called_names(upgrade)
        downgrade = _function_body(tree, "downgrade")
        revoked = _called_names(downgrade) if downgrade is not None else set()
        for grant_fn, revoke_fn in _PARTITION_GRANT_PAIRS:
            if grant_fn in granted and revoke_fn not in revoked:
                offenders.append(f"{path.name}：缺少 {revoke_fn}()")
    return offenders


def test_partition_grant_has_revoke_counterpart_in_downgrade() -> None:
    """每个在 upgrade 里授予分区权限的迁移，downgrade 必须调用对偶撤销。"""
    offenders = _asymmetric_migrations()
    assert offenders == [], (
        "以下迁移在 upgrade 里授予分区权限，但 downgrade 缺少对偶撤销——分区 ACL "
        "独立于父表，只撤父表会让 DROP ROLE 失败：\n"
        + "\n".join(f"  - {item}" for item in offenders)
    )


def test_grant_and_revoke_share_one_partition_predicate() -> None:
    """授予/撤销两个 DO 块必须共用同一段分区谓词（否则覆盖集迟早不同）。

    ``relkind = 'r'`` 这一条尤其不能丢：分区上的索引同样 ``relispartition``，
    对着索引 GRANT/REVOKE 会报「… is an index」。
    """
    grant = ops_partition_grant_do_block()
    revoke = ops_partition_revoke_do_block()
    predicate = (
        "        SELECT c.relname\n"
        "        FROM pg_class c\n"
        "        JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "        WHERE n.nspname = 'outbox'\n"
        "          AND c.relispartition\n"
        "          AND c.relkind = 'r'\n"
    )
    assert predicate in grant
    assert predicate in revoke
    # 撤销侧只允许一条语句（REVOKE ALL），多出来的授予语句说明有人把两侧抄混了。
    assert grant.count("EXECUTE format(") == 2
    assert revoke.count("EXECUTE format(") == 1


@pytest.mark.parametrize("fn", [ops_partition_grant_do_block, ops_partition_revoke_do_block])
def test_do_blocks_are_single_statement(fn: object) -> None:
    """生成的 DO 块必须是一条完整语句（alembic 逐条 execute，不能带尾分号垃圾）。"""
    sql = fn()  # type: ignore[operator]
    assert sql.startswith("DO $$")
    assert sql.endswith("$$")
    assert "END LOOP;" in sql
