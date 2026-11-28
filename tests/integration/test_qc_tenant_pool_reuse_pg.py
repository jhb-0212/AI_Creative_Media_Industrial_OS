"""租户上下文的**连接池复用**运行期验证（ADR-R40 验收项 E1.5）。

为什么这条不能用「同一连接 A→B→RESET」实验替代
----------------------------------------------
仓库里能查到的证据是「显式 RESET 之后没有残留」。生产代码没有 RESET：它依赖
``set_config(..., true)`` 的事务级生效，然后**归还连接**、由池分给下一个请求。
风险恰好在这段没有断言的地方——若某次事务没有真正回滚/结束，下一个借用者
会继承上一个租户的 GUC，并且看起来一切正常（只会少看到或多看到行）。

所以本用例把池压到**恰好一个物理连接**（``pool_size=1, max_overflow=0``），
逐条断言 ``pg_backend_pid`` 相同（证明真的复用了同一连接），再断言
``app.current_tenant_id`` 与可见行数。四条序列覆盖：提交、回滚、连续两租户、
Worker 式逐租户循环。

失败信号必须是显式断言：每次借出都先读 GUC，再读两行已知数据；只看「查不到」
会把「策略拒绝」和「数据本来就没有」混为一谈。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import (
    build_qc_governance_database_url,
    create_qc_governance_engine,
)
from packages.shared.rls import RLS_TENANT_SETTING, set_tenant_context
from tests.integration.qc_seed import seed_tenant_task

_TENANT_SQL = text(
    "SELECT NULLIF(current_setting(:key, true), '') AS tenant, pg_backend_pid() AS pid"
)

# 一次「借出 → 读 GUC/pid → 按租户数行」的最小动作；所有序列都走它，避免
# 每条用例各写一份 SQL 而在某一份里悄悄少了 GUC 断言。
_COUNT_SQL = text("SELECT count(*) FROM qc_governance.qc_tasks WHERE tenant_id = :tenant")


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """常规池（默认容量）：只用于播种，不参与「复用」断言。"""
    engine = create_qc_governance_engine()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def single_connection_pool() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    """池容量 1 的 qc_governance 池：强制每次借出都拿到同一物理连接。"""
    engine = create_async_engine(
        build_qc_governance_database_url(),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=False,
    )
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _read_context(session: AsyncSession) -> tuple[str | None, int]:
    row = (await session.execute(_TENANT_SQL, {"key": RLS_TENANT_SETTING})).one()
    return row.tenant, int(row.pid)


async def _count(session: AsyncSession, tenant: uuid.UUID) -> int:
    return int(await session.scalar(_COUNT_SQL, {"tenant": tenant}))


async def test_committed_transaction_does_not_leak_context(
    single_connection_pool, qc_sessionmaker, admin_sessionmaker
) -> None:
    """序列 1：A 注入并提交 → 归还 → 下一个事务不注入，GUC 必须为空、A 行不可见。"""
    _engine, sm = single_connection_pool
    tenant_a, _ = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    async with sm() as session:
        await set_tenant_context(session, tenant_a)
        ctx, pid_a = await _read_context(session)
        assert ctx == str(tenant_a)
        assert await _count(session, tenant_a) == 1
        await session.commit()

    async with sm() as session:
        ctx, pid_b = await _read_context(session)
        assert pid_b == pid_a, "池只有一条连接，pid 变了说明这条用例没测到复用"
        assert ctx is None, "提交后归还的连接不得带着上一个租户的上下文"
        assert await _count(session, tenant_a) == 0, (
            "没有租户上下文时应当一行都看不到；能看到说明 RLS 对这条连接没生效"
        )


async def test_rolled_back_transaction_does_not_leak_context(
    single_connection_pool, qc_sessionmaker, admin_sessionmaker
) -> None:
    """序列 2：A 注入后异常回滚 → 借出 → 断言无残留。

    回滚是最容易「看起来干净」的路径：事务里的注入本来就该随事务消失，
    但如果实现把 ``set_config`` 的第三个参数写成 ``false``（会话级），回滚
    不会清除它，本用例就会红。
    """
    _engine, sm = single_connection_pool
    tenant_a, _ = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    async with sm() as session:
        await set_tenant_context(session, tenant_a)
        try:
            await _count(session, tenant_a)
            raise RuntimeError("boom：模拟事务中途失败")
        except RuntimeError:
            await session.rollback()

    async with sm() as session:
        ctx, _pid = await _read_context(session)
        assert ctx is None, "回滚的事务不得把租户上下文留给下一个借用者"
        assert await _count(session, tenant_a) == 0


async def test_consecutive_tenants_see_only_their_own_rows(
    single_connection_pool, qc_sessionmaker, admin_sessionmaker
) -> None:
    """序列 3：A 事务 → B 事务，B 只看得到 B，且看不到 A。"""
    _engine, sm = single_connection_pool
    tenant_a, _task_a = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)
    tenant_b, _task_b = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    async with sm() as session:
        await set_tenant_context(session, tenant_a)
        assert await _count(session, tenant_a) == 1
        assert await _count(session, tenant_b) == 0, "A 的会话里不得看到 B 的行"
        await session.commit()

    async with sm() as session:
        await set_tenant_context(session, tenant_b)
        ctx, _pid = await _read_context(session)
        assert ctx == str(tenant_b)
        assert await _count(session, tenant_b) == 1
        assert await _count(session, tenant_a) == 0, "换了租户不得残留上一租户的可见性"


async def test_worker_style_tenant_loop_does_not_inherit(
    single_connection_pool, qc_sessionmaker, admin_sessionmaker
) -> None:
    """序列 4：Worker 式「逐租户短事务」循环，断言下一租户不继承上一租户。"""
    _engine, sm = single_connection_pool
    tenant_a, _ = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)
    tenant_b, _ = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    pids: list[int] = []
    for tenant in (tenant_a, tenant_b, tenant_a):
        async with sm() as session:
            await set_tenant_context(session, tenant)
            ctx, pid = await _read_context(session)
            pids.append(pid)
            assert ctx == str(tenant), f"本轮的上下文必须是 {tenant}"
            assert await _count(session, tenant) == 1
            other = tenant_b if tenant == tenant_a else tenant_a
            assert await _count(session, other) == 0
            await session.commit()

    assert len(set(pids)) == 1, (
        "三轮必须落在同一条物理连接上；pid 不一致说明「复用」这个前提没成立，"
        "用例就退化成了三条互不相干的连接"
    )
