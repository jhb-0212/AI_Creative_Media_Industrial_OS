"""项目/分集生命周期事件与幂等冲突（P2-1 / P3-1）。

证明：
1. 创建项目/分集在同一事务里写入 PROJECT_CREATED / EPISODE_CREATED；
2. 同 entity_id 复用于不同内容 → 409 IDEMPOTENCY_CONFLICT（不是静默返回旧对象）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url
from tests.conftest import TEST_TENANT_ID


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _event_count(
    engine: AsyncEngine, event_type: str, aggregate_id: str
) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox.outbox_events "
                        "WHERE tenant_id = :t AND event_type = :e "
                        "AND aggregate_id = :a"
                    ),
                    {"t": TEST_TENANT_ID, "e": event_type, "a": aggregate_id},
                )
            ).scalar_one()
        )


async def test_project_and_episode_creation_emit_events(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    project_id = f"proj_evt_{uuid.uuid4().hex[:8]}"
    resp = await auth_client.post(
        "/projects",
        json={
            "project_name": "事件项目",
            "project_tier": "A",
            "entity_id": project_id,
        },
    )
    assert resp.status_code == 201, resp.text
    assert await _event_count(admin_engine, "PROJECT_CREATED", project_id) == 1

    episode_id = f"ep_evt_{uuid.uuid4().hex[:8]}"
    resp = await auth_client.post(
        "/episodes",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "第一集",
            "entity_id": episode_id,
        },
    )
    assert resp.status_code == 201, resp.text
    assert await _event_count(admin_engine, "EPISODE_CREATED", episode_id) == 1


async def test_project_idempotency_key_reuse_conflicts(
    auth_client: AsyncClient,
) -> None:
    project_id = f"proj_idemconflict_{uuid.uuid4().hex[:8]}"
    first = await auth_client.post(
        "/projects",
        json={
            "project_name": "原始名称",
            "project_tier": "B",
            "entity_id": project_id,
        },
    )
    assert first.status_code == 201

    second = await auth_client.post(
        "/projects",
        json={
            "project_name": "另一个名称",
            "project_tier": "B",
            "entity_id": project_id,
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
