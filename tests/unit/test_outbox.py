"""Transactional Outbox 审计事件的集成测试（决策 3 + R7）。

核心验证：
1. 状态变更（导入/锁定/取代）会同步写入不可变 outbox 事件。
2. 事件与业务状态在同一事务原子提交（状态落库 = 事件落库）。
3. 事件 append-only：多次状态迁移产生多条事件，而非覆盖（替代 audit_ref 单字段）。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from apps.api.main import app
from packages.shared.database import get_sessionmaker


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _setup_project_and_episode(
    client: AsyncClient, *, project_id: str, episode_ref: str
) -> None:
    await client.post(
        "/projects",
        json={
            "project_name": f"项目_{project_id}",
            "project_tier": "B",
            "created_by": "user_owner",
            "entity_id": project_id,
        },
    )
    await client.post(
        "/episodes",
        json={
            "project_id": project_id,
            "episode_ref": episode_ref,
            "title": f"集数_{episode_ref}",
            "created_by": "user_owner",
        },
    )


async def _count_events(aggregate_id: str) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM outbox.outbox_events WHERE aggregate_id = :aid"
            ),
            {"aid": aggregate_id},
        )
        return result.scalar_one()


async def test_import_writes_outbox_event(client: AsyncClient) -> None:
    """导入剧本产生 SCRIPT_IMPORTED 事件。"""
    project_id = f"proj_ob_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_ref="EP001")

    script_id = f"scr_ob_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "审计测试剧本",
            "created_by": "user_script",
            "entity_id": script_id,
        },
    )
    assert resp.status_code == 201

    # 事件已落库（与状态同事务提交）。
    assert await _count_events(script_id) == 1


async def test_lock_writes_outbox_event_append_only(client: AsyncClient) -> None:
    """导入 + 锁定产生两条事件（append-only，非覆盖），证明 R7 可追溯。"""
    project_id = f"proj_ob2_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_ref="EP001")

    script_id = f"scr_ob2_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "审计测试剧本2",
            "created_by": "user_script",
            "entity_id": script_id,
        },
    )
    assert resp.status_code == 201
    assert await _count_events(script_id) == 1  # SCRIPT_IMPORTED

    # 锁定：DRAFT→LOCKED，追加第二条事件（SCRIPT_LOCK_APPROVED）。
    resp2 = await client.post(
        f"/scripts/{script_id}/lock",
        json={"requested_by": "编剧A", "approved_by": "导演B"},
    )
    assert resp2.status_code == 200

    # 两条事件都保留（append-only，不覆盖），可完整追溯导入→锁定历史。
    assert await _count_events(script_id) == 2

    # 验证事件类型正确。
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT event_type FROM outbox.outbox_events "
                "WHERE aggregate_id = :aid ORDER BY occurred_at"
            ),
            {"aid": script_id},
        )
        types = [row[0] for row in result.fetchall()]
    assert types == ["SCRIPT_IMPORTED", "SCRIPT_LOCK_APPROVED"]
