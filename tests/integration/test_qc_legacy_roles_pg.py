"""遗留角色 ``afs_audit`` / ``afs_data_repair`` 已退役，本文件钉住「不得复活」。

它们是谁、为什么退役
--------------------
``data/migrations/pending/feat-ownership`` 那套未合并设计里的审计 / 数据修复
身份。主迁移链从未创建它们，干净库里也不存在。退役前取证发现：开发主库零引用，
但三个实验库（``ai_film_industrial`` / ``t5_lab`` / ``lab_rework_tasks``）仍以
95 / 86 / 95 条 ``pg_shdepend`` 依赖挂着它们——除 ``audit_quarantine_scope`` /
``repair_row_scope`` 策略外，还有 43 / 38 / 43 条 ``quarantine_restrictive_*``
策略在表达式里调 ``pg_has_role(..., 'afs_data_repair')``。这类「只在表达式里
出现」的引用不产生 ``pg_shdepend`` 行，PG 会放行 ``DROP ROLE``，然后那些策略
在每次查询时报「角色不存在」——正是 ``scripts/retire_legacy_roles.py`` 把
「可读引用」与「影子依赖」两个来源都判阻塞的原因。

处置：2026-11-26 逐库删掉上述策略、``DROP OWNED BY`` 撤销授权，``--check`` 归零
后 ``--drop``，两个角色在集群级消失。

为什么留一条「不存在」的断言
----------------------------
退役是可重建的（策略定义仍在 pending 文件里）。所以真正要防的不是「今天它不在」，
而是「有人在某个分支里又把它建回来，却没人记得这是被退役过的身份」。``pg_roles``
是集群共享视图，这条断言因而覆盖整台服务器，而不只是当前库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RETIRED_ROLES = ("afs_audit", "afs_data_repair")

# pg_shdepend 是 ``DROP ROLE`` 的权威判据；不按 dbid 过滤，即全集群口径。
_CLUSTER_DEPENDENCIES_SQL = text(
    "SELECT count(*) FROM pg_shdepend d "
    "JOIN pg_roles r ON r.oid = d.refobjid "
    "WHERE d.refclassid = 'pg_authid'::regclass "
    "  AND r.rolname = ANY(:roles)"
)


async def test_retired_legacy_roles_are_gone(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """两个角色必须不存在（``pg_roles`` 覆盖全集群）。

    重建它们要走治理流程：改本文件、在注册表里重新登记、说明为什么需要一个新的
    审计 / 修复身份。这条用例就是把「悄悄建回来」变成红灯。
    """
    async with admin_sessionmaker() as session:
        survivors = (
            await session.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                {"roles": list(_RETIRED_ROLES)},
            )
        ).scalars()
        found = list(survivors)
    assert not found, (
        "已退役的遗留角色重新出现在集群里："
        f"{', '.join(found)}；重建需走治理流程（注册表登记 + 评审）"
    )


async def test_no_cluster_dependencies_on_retired_roles(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """全集群没有任何对象依赖这两个名字：退役不会让别的库在查询时才崩。

    ``pg_shdepend`` 为空不等于绝对干净（只在表达式里出现的引用不记账），所以
    上一轮清库还额外删了 ``pg_has_role`` 表达式策略；这条用例保证「至少没有
    PG 会拦的依赖」，配合 ``retire_legacy_roles.py --check`` 的可读引用检查才是
    完整口径。
    """
    async with admin_sessionmaker() as session:
        count = await session.scalar(
            _CLUSTER_DEPENDENCIES_SQL, {"roles": list(_RETIRED_ROLES)}
        )
    assert count == 0, "集群里仍有对象依赖已退役角色，请用 retire_legacy_roles.py 定位"


@pytest.mark.parametrize("role", _RETIRED_ROLES)
async def test_retired_role_name_is_not_usable_for_privilege_probes(
    admin_sessionmaker: async_sessionmaker[AsyncSession], role: str
) -> None:
    """以已退役名字探测权限必须报错，而不是静默返回 False。

    这条是给「顺手写 ``has_table_privilege('afs_audit', ...)`` 的后来者」的
    防呆：角色不存在时 PG 抛 ``undefined_object``，一个 try/except 就变成
    「没有权限」的假阴性；这里把该行为显式记录下来，改回角色时必须同步改这里。
    """
    async with admin_sessionmaker() as session:
        with pytest.raises(Exception) as excinfo:
            await session.scalar(
                text("SELECT has_schema_privilege(:r, 'qc_governance', 'USAGE')"),
                {"r": role},
            )
    assert "does not exist" in str(excinfo.value).lower()
