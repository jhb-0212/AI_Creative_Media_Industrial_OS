"""两个遗留角色的边界锁定：``afs_audit`` / ``afs_data_repair`` 对 QC 面零触达。

为什么不删它们
--------------
它们在 WP-A 之前承载过审计与数据修复的临时权力，本轮之后已被四个维护角色
（owner / operator / login / scheduler）+ 命令进程取代。看起来该退役，但：

1. ``DROP ROLE`` 在多库授过权的集群里会直接失败（本轮已实测：``downgrade``
   里删角色导致迁移回滚中断，最终改为「回结构不删身份」）；
2. 删角色是不可逆的正式库变更，属独立治理动作，不该混在代码批次里；
3. 真正要防的不是「这个名字还在」，而是「它还能碰到新边界」。

所以处置是：**保权不删，并用断言钉死现状**。测试只证明「现在它碰不到」，
一旦谁在迁移里给它补一条 GRANT，这条用例立刻红——那正是需要被看见的时刻。
它不表达「永远不删」，也不假装这两个角色已被清理。
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
