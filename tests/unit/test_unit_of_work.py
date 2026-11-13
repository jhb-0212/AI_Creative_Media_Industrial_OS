"""工作单元（UoW）边界语义测试（R07）。

验证单一实现的四条不变量：
1. 显式 commit → 持久化（Worker 与 API 同一原语，不依赖请求上下文）。
2. 异常退出 → 全量回滚，不留下半条业务/事件。
3. 忘记 commit → 不落库（fail-closed，杜绝「静默成功」）。
4. 只读单元 → 数据库层拒绝写语句（GET 不可能误写）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.unit_of_work import unit_of_work
from tests.conftest import TEST_TENANT_ID


async def _insert_event(
    session: AsyncSession, *, aggregate_id: str, event_id: uuid.UUID
) -> None:
    """插入一条 outbox 事件（不提交，提交语义由被测工作单元决定）。"""
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :t, true)"),
        {"t": str(TEST_TENANT_ID)},
    )
    await session.execute(
        text(
            "INSERT INTO outbox.outbox_events "
            "(event_id, aggregate_type, aggregate_id, tenant_id, event_type, "
            "payload, occurred_at) VALUES "
            "(:eid, 'UoWTest', :aid, :tid, 'UOW_TEST', '{}'::jsonb, now())"
        ),
        {
            "eid": event_id,
            "aid": aggregate_id,
            "tid": TEST_TENANT_ID,
        },
    )


async def _count_events(
    sessionmaker: async_sessionmaker[AsyncSession], *, aggregate_id: str
) -> int:
    async with sessionmaker() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(TEST_TENANT_ID)},
        )
        result = await session.execute(
            text("SELECT count(*) FROM outbox.outbox_events WHERE aggregate_id = :aid"),
            {"aid": aggregate_id},
        )
        return result.scalar_one()


async def test_worker_unit_of_work_commits_persistently(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Worker 场景：不依赖 request，同一原语显式 commit 后数据可见。"""
    aggregate_id = f"uow-worker-{uuid.uuid4().hex[:8]}"

    async with unit_of_work(db_sessionmaker) as uow:
        await _insert_event(
            uow.session, aggregate_id=aggregate_id, event_id=uuid.uuid4()
        )
        await uow.commit()

    assert await _count_events(db_sessionmaker, aggregate_id=aggregate_id) == 1


async def test_unit_of_work_rolls_back_on_failure(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """异常路径：业务变更与事件一起回滚，不留半条数据。"""
    aggregate_id = f"uow-fail-{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError):
        async with unit_of_work(db_sessionmaker) as uow:
            await _insert_event(
                uow.session, aggregate_id=aggregate_id, event_id=uuid.uuid4()
            )
            raise RuntimeError("worker crashed mid-transaction")

    assert await _count_events(db_sessionmaker, aggregate_id=aggregate_id) == 0


async def test_unit_of_work_without_commit_does_not_persist(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """忘记提交 = 不落库（fail-closed），不允许「响应成功但数据丢失」。"""
    aggregate_id = f"uow-nocommit-{uuid.uuid4().hex[:8]}"

    async with unit_of_work(db_sessionmaker) as uow:
        await _insert_event(
            uow.session, aggregate_id=aggregate_id, event_id=uuid.uuid4()
        )

    assert await _count_events(db_sessionmaker, aggregate_id=aggregate_id) == 0


async def test_read_only_unit_rejects_writes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """只读单元：INSERT 被 PostgreSQL 拒绝（GET 用例无法误写）。"""
    aggregate_id = f"uow-readonly-{uuid.uuid4().hex[:8]}"

    with pytest.raises(DBAPIError, match="read-only"):
        async with unit_of_work(db_sessionmaker, read_only=True) as uow:
            await _insert_event(
                uow.session, aggregate_id=aggregate_id, event_id=uuid.uuid4()
            )

    assert await _count_events(db_sessionmaker, aggregate_id=aggregate_id) == 0
