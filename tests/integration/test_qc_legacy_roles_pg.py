"""两个遗留角色的边界锁定：``afs_audit`` / ``afs_data_repair`` 对 QC 面零触达。

它们是谁
--------
``data/migrations/pending/feat-ownership`` 那套未合并设计里的审计 / 数据修复
身份（策略名 ``audit_quarantine_scope`` / ``repair_row_scope``、函数
``ownership_repair_allows`` 都指向它们）。主迁移链**从未创建**它们，干净库
（如 ``ai_film_pg18_repair``）里也不存在——它们只活在开发集群上，是手工实验
残留。

为什么先不删
------------
``DROP ROLE`` 是集群级操作。实测取证（``scripts/retire_legacy_roles.py
--check``）显示：``ai_film_api_tests`` 本库零引用，但有三个实验库仍在用它们
（``ai_film_industrial`` / ``t5_lab`` / ``lab_rework_tasks``，各 95 / 86 / 95
条影子依赖）。因此退役动作被拆成两步：本仓库先把**判定与执行工具**落地
（``--check`` 可复核、``--drop`` 只在零引用时动手），清实验库那一步留给一次
显式的治理决定。

本文件钉的是第二条底线：**无论删不删，它们都碰不到 QC 面**。一旦谁在迁移里
给它们补一条 GRANT，这条用例立刻红——那正是需要被看见的时刻。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_LEGACY_ROLES = ("afs_audit", "afs_data_repair")

_QC_FUNCTIONS_SQL = text(
    "SELECT p.oid, n.nspname, p.proname "
    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname IN ('qc_maintenance', 'qc_governance')"
)

# ``DROP ROLE`` 的真实判据是 pg_shdepend：它一旦非空，删除就会失败并中断
# 迁移。这里按「本库 + 共享对象」过滤——其他库的依赖属于集群级退役动作，
# 不该让本库的用例替它们变红。
_THIS_DATABASE_SHADOW_DEPS_SQL = text(
    "SELECT count(*) FROM pg_shdepend d "
    "JOIN pg_roles r ON r.oid = d.refobjid "
    "WHERE d.refclassid = 'pg_authid'::regclass "
    "  AND r.rolname = ANY(:roles) "
    "  AND (d.dbid = 0 OR d.dbid = (SELECT oid FROM pg_database "
    "                              WHERE datname = current_database()))"
)


@pytest.mark.parametrize("role", _LEGACY_ROLES)
async def test_legacy_role_is_not_a_login_and_has_no_qc_surface(
    admin_sessionmaker: async_sessionmaker[AsyncSession], role: str
) -> None:
    async with admin_sessionmaker() as session:
        exists = (
            await session.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            )
        ).first()
        if exists is None:
            # 干净库（如 PG18 验收库）从未创建过这两个角色：断言不适用，
            # 跳过而不是失败。这同时说明「新库不会再长出它们」。
            pytest.skip(f"{role} 在该库不存在（干净库未创建遗留角色）")
        attrs = (
            await session.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = :r"
                ),
                {"r": role},
            )
        ).one()
        assert attrs.rolcanlogin is False, "遗留角色不应还能登录"
        assert attrs.rolsuper is False
        assert attrs.rolbypassrls is False, "任何维护面角色都不得绕过 RLS"

        assert not await session.scalar(
            text("SELECT has_schema_privilege(:r,'qc_governance','USAGE')"),
            {"r": role},
        ), "遗留角色不得有 qc_governance 的 schema USAGE"
        assert not await session.scalar(
            text("SELECT has_schema_privilege(:r,'qc_maintenance','USAGE')"),
            {"r": role},
        ), "遗留角色不得有 qc_maintenance 的 schema USAGE"
        for table in (
            "qc_governance.qc_tasks",
            "qc_maintenance.task_projection",
        ):
            assert not await session.scalar(
                text("SELECT has_table_privilege(:r,:t,'SELECT')"),
                {"r": role, "t": table},
            ), f"遗留角色不得直读 {table}"

        # 函数按 **OID** 逐个判定，不按 ``name(text,text)`` 拼参数串：签名会随
        # 迁移演进（补记函数就从 4 参变 5 参），一旦拼错，Postgres 报的是
        # 「函数不存在」而不是「没有权限」——用例会红，但红的原因和要防的事
        # 无关。全 schema 扫描还能顺带覆盖触发器函数（历史里恰有一只为
        # PUBLIC 留了 EXECUTE 的判定函数）。
        for oid, schema, name in (
            await session.execute(_QC_FUNCTIONS_SQL)
        ).all():
            assert not await session.scalar(
                text(
                    "SELECT has_function_privilege(:r, CAST(:oid AS oid), 'EXECUTE')"
                ),
                {"r": role, "oid": oid},
            ), f"遗留角色不得执行 {schema}.{name}"


async def test_legacy_roles_have_no_dependency_in_this_database(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """本库对两个遗留角色零依赖：迁移与运行时都不会因它们被删而中断。

    这条用例与上一个互补：上一个证明「碰不到 QC 面」（禁用面），这条证明
    「本库根本没接住它们」（可退役面）。分开写是因为它们将来会分开演进——
    实验库清完、角色删掉之后，上一个用例在开发库上会变成 skip，而这条
    应当继续成立（零依赖是永恒的）。
    """
    async with admin_sessionmaker() as session:
        count = await session.scalar(_THIS_DATABASE_SHADOW_DEPS_SQL, {"roles": list(_LEGACY_ROLES)})
    assert count == 0, (
        "本库出现了对遗留角色的依赖（授权、策略或成员关系）："
        "退役工具会把本库判为阻塞，请先定位来源"
    )
