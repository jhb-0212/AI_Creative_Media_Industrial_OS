"""数据库层不变量：租户隔离与 append-only 授权必须"在结构里成立"。

对应 ADR-R10（权限基线反转）与 ADR-R06-2 的修订结论。为什么不用"再补一条
RLS 负向测试"来解决，而要单独立这组断言：

- 旧测试只覆盖 ``project_planning`` 三张表（``test_rls_isolation.py``），
  分区是**动态创建**的对象，任何"逐表手写用例"的写法都会在新分区出现时失效；
- 因此这里断言的是**规律**而不是某张表：凡是 outbox 分区，必须
  ① ``relrowsecurity=true``、② 有租户策略、③ app_role 无任何直连权限；
- append-only 对象的授权口径与行为**不在这里**：口径由
  ``test_least_privilege_grants.py`` 的矩阵对账（表级 + 列级 + 继承）判定，
  行为由 ``test_qc_reports_are_append_only`` /
  ``test_asset_role_cannot_delete_asset_versions`` 判定。此前这里另有一份
  「授权集合 == 硬编码期望」的用例，它是同一事实的第三处声明（期望值与矩阵
  会各自漂移），已按 C1 裁决删除；删除的依据不是「ACL 不重要」，而是判据
  应当归属于唯一事实源。

这些断言在 CI 里跑（依赖真实 PostgreSQL），是"分区保护钩子"生效与否的
唯一可信证据——迁移/维护工具改坏了，这里会红。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import (
    build_database_url,
    build_migration_url,
)
from packages.shared.db_roles import app_role_name
from packages.shared.partition_guard import (
    OUTBOX_PARENT,
    OUTBOX_SCHEMA,
    PARTITION_POLICY,
    quote_ident,
)

_LIST_PARTITIONS = sa.text(
    """
    SELECT c.relname
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    JOIN pg_namespace n ON n.oid = p.relnamespace
    WHERE n.nspname = :schema AND p.relname = :parent
    ORDER BY c.relname
    """
)

_PARTITION_STATE = sa.text(
    """
    SELECT c.relrowsecurity AS rls_enabled
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND c.relname = :name
    """
)


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """迁移角色连接：只用于读系统目录（业务代码不接触）。"""
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """app_role 连接（拆角色前的旧应用角色，分区直连权限已收口）。

    用于验证「app_role 直连分区被拒」——app_role 的业务 schema 权限已被
    DATA-1 迁移 8f3b2c1d4e5a 回收，但分区的「直连拒绝」是独立的收口口径
    （protect_partition_statements 的 REVOKE ALL），仍需独立验证。
    """
    engine = create_async_engine(build_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _partitions(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            _LIST_PARTITIONS, {"schema": OUTBOX_SCHEMA, "parent": OUTBOX_PARENT}
        )
        return [str(name) for name in rows.scalars()]


async def _direct_privileges(session: AsyncSession, table: str) -> set[str]:
    """app_role 在该表上的直连权限（不含通过父表继承的能力）。"""
    rows = await session.execute(
        sa.text(
            """
            SELECT privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = :role AND table_schema = :schema AND table_name = :table
            """
        ),
        {"role": app_role_name(), "schema": OUTBOX_SCHEMA, "table": table},
    )
    return {str(privilege) for privilege in rows.scalars()}


async def test_every_outbox_partition_is_protected(
    admin_engine: AsyncEngine,
) -> None:
    """每个分区都必须开启 RLS、带租户策略，且 app_role 不能直连。"""
    partitions = await _partitions(admin_engine)
    assert partitions, "outbox.outbox_events 没有任何分区，环境不完整"

    async with admin_engine.connect() as conn:
        for partition in partitions:
            rls = (
                await conn.execute(
                    _PARTITION_STATE, {"schema": OUTBOX_SCHEMA, "name": partition}
                )
            ).scalar_one()
            assert rls is True, f"分区 {partition} 未开启 RLS"

            policy_names = (
                await conn.execute(
                    sa.text(
                        """
                        SELECT policyname
                        FROM pg_policies
                        WHERE schemaname = :schema AND tablename = :name
                        """
                    ),
                    {"schema": OUTBOX_SCHEMA, "name": partition},
                )
            ).scalars()
            assert PARTITION_POLICY in set(policy_names), (
                f"分区 {partition} 缺少租户策略 {PARTITION_POLICY}"
            )

    sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        for partition in partitions:
            privileges = await _direct_privileges(session, partition)
            assert privileges == set(), (
                f"分区 {partition} 仍对 {app_role_name()} 直连授权: {sorted(privileges)}"
            )


async def test_direct_partition_access_is_denied_for_app_role(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    """app_role 直接点名分区必须被拒绝（不能靠"没人会这么写"来防守）。"""
    partitions = await _partitions(admin_engine)
    assert partitions

    target = f"{quote_ident(OUTBOX_SCHEMA)}.{quote_ident(partitions[-1])}"
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(sa.text(f"SELECT count(*) FROM {target}"))
    assert "permission denied" in str(exc_info.value).lower()


async def test_outbox_parent_still_fails_closed_without_tenant(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """父表路径保持原语义：无租户上下文时 0 行（应用不因本次收紧而失效）。"""
    async with db_sessionmaker() as session:
        count = await session.scalar(sa.text("SELECT count(*) FROM outbox.outbox_events"))
    assert count == 0
