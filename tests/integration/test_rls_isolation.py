"""RLS 跨租户隔离负向测试（证明业务域角色下 RLS 真实生效）。

安全边界（决策 5，务必理解为何不加 FORCE RLS）：
- 迁移 8f2a1b3c4d5e 对 projects / episodes / script_revisions 启用 RLS；
  ADR-R24 之后策略是**双维度**（租户 + 组织）。本文件的用例不注组织上下文，
  写入行的 ``organization_id`` 为 NULL，正好落在 ``IS NULL OR …`` 的第一支。
- 应用与测试以业务域角色（``project_planning_role`` / ``asset_service_role``，
  DATA-1）连库；这些角色是「非表 owner + NOSUPERUSER + NOBYPASSRLS」，因此
  RLS 对其真实生效。``app_role`` 已不再被任何业务域使用。
- 表 owner 是 postgres **超级用户**，而超级用户永远绕过 RLS——所以
  ``FORCE ROW LEVEL SECURITY`` 在本拓扑下不会产生任何额外约束（FORCE 只
  约束非超级用户的 owner）。真正的防呆是「应用绝不以 owner/superuser 连接」，
  该不变量由 DSN 构造固定使用域角色保证，并由启动期 ``verify_rls_enforcement``
  在接入流量前拒绝超级用户连接（ADR-R24 §D3a-5）。
- 删除语义（DATA-6）：域角色**没有** DELETE 权限，物理删除在提交前就被数据库
  拒绝；「RLS 把 DELETE 过滤成 0 行」这条路已无角色可走，故本文件改为断言
  权限拒绝（42501）。它比 RLS 过滤更强：即使某天策略被误改成放行，也没有
  删除能力。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.api.main import app
from tests.conftest import set_test_scope

# 两个互不相干的租户（随机 UUID，避免与历史数据冲突）。
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()

_INSERT_PROJECT = text(
    """
    INSERT INTO project_planning.projects
        (entity_id, entity_type, schema_version, entity_version, status,
         owner_module, tenant_id, created_at, created_by, updated_at,
         project_name, project_tier)
    VALUES
        (:entity_id, 'Project', 205, 1, 'DRAFT', 'project_planning',
         :tenant_id, :now, 'rls_tester', :now, :name, 'B')
    """
)


async def _insert_project(sm, tenant_id, entity_id: str, name: str) -> None:
    """在指定租户上下文内插入一行项目。"""
    async with sm() as session:
        await set_test_scope(session, tenant_id=tenant_id)
        await session.execute(
            _INSERT_PROJECT,
            {
                "entity_id": entity_id,
                "tenant_id": tenant_id,
                "now": datetime.now(UTC),
                "name": name,
            },
        )
        await session.commit()


async def _delete_project(admin_sessionmaker, entity_id: str) -> None:
    """清理一行项目（**管理连接**：域角色没有 DELETE 权限，见 DATA-6）。

    清理是测试基础设施，不是业务路径。为让清理能跑而去给域角色补 DELETE，
    就是把「删除语义是 ARCHIVED 而非物理删除」这条契约改掉——方向反了。
    """
    async with admin_sessionmaker() as session:
        await session.execute(
            text("DELETE FROM project_planning.projects WHERE entity_id = :e"),
            {"e": entity_id},
        )
        await session.commit()


async def test_cross_tenant_insert_rejected() -> None:
    """租户 A 上下文写入 tenant_id=B 的行 → 被 RLS WITH CHECK 拒绝。"""
    sm = app.state.sessionmaker
    entity_id = f"rls_bad_{uuid.uuid4().hex[:8]}"
    with pytest.raises(DBAPIError) as exc_info:
        async with sm() as session:
            await set_test_scope(session, tenant_id=TENANT_A)
            await session.execute(
                _INSERT_PROJECT,
                {
                    "entity_id": entity_id,
                    "tenant_id": TENANT_B,
                    "now": datetime.now(UTC),
                    "name": "跨租户写入",
                },
            )
            await session.commit()
    message = str(exc_info.value).lower()
    assert "row-level security" in message or "row level security" in message


async def test_cross_tenant_rows_invisible_and_not_writable(
    admin_sessionmaker,
) -> None:
    """A 看不到 B 的行；A 对 B 行的 UPDATE/DELETE 影响 0 行。"""
    sm = app.state.sessionmaker
    eid_a = f"rls_a_{uuid.uuid4().hex[:8]}"
    eid_b = f"rls_b_{uuid.uuid4().hex[:8]}"
    try:
        await _insert_project(sm, TENANT_A, eid_a, "A 租户项目")
        await _insert_project(sm, TENANT_B, eid_b, "B 租户项目")

        # 1) A 上下文只能看到自己的行（B 的行被 RLS 过滤）。
        async with sm() as session:
            await set_test_scope(session, tenant_id=TENANT_A)
            visible = await session.scalar(
                text(
                    "SELECT count(*) FROM project_planning.projects "
                    "WHERE entity_id IN (:a, :b)"
                ),
                {"a": eid_a, "b": eid_b},
            )
        assert visible == 1

        # 2) A 上下文 UPDATE B 的行 → 0 行受影响。
        async with sm() as session:
            await set_test_scope(session, tenant_id=TENANT_A)
            result = await session.execute(
                text(
                    "UPDATE project_planning.projects SET project_name = '越权改名' "
                    "WHERE entity_id = :e"
                ),
                {"e": eid_b},
            )
            await session.commit()
        assert result.rowcount == 0

        # 3) A 上下文 DELETE → 数据库直接拒绝（42501，无 DELETE 权限）。
        #    这比「RLS 过滤成 0 行」更强：删除语义是 ARCHIVED/SUPERSEDED，
        #    契约里没有 *.DELETE 动作，域角色也就不该有这项能力。
        async with sm() as session:
            await set_test_scope(session, tenant_id=TENANT_A)
            with pytest.raises(DBAPIError) as exc_info:
                await session.execute(
                    text("DELETE FROM project_planning.projects WHERE entity_id = :e"),
                    {"e": eid_b},
                )
            assert "42501" in str(exc_info.value) or "permission denied" in str(
                exc_info.value
            ).lower()
            await session.rollback()

        # 4) B 的行仍在，且 B 上下文可见（A 的删除未生效）。
        async with sm() as session:
            await set_test_scope(session, tenant_id=TENANT_B)
            count_b = await session.scalar(
                text(
                    "SELECT count(*) FROM project_planning.projects "
                    "WHERE entity_id = :e"
                ),
                {"e": eid_b},
            )
        assert count_b == 1
    finally:
        await _delete_project(admin_sessionmaker, eid_a)
        await _delete_project(admin_sessionmaker, eid_b)
