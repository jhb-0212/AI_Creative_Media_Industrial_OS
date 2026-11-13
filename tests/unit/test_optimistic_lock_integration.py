"""乐观锁并发控制的集成测试（连真实 PostgreSQL，验证数据库层条件更新）。

核心验证：``conditional_update`` 的 ``WHERE entity_version = :expected`` 在
并发下原子串行化，第二个基于陈旧版本的写操作命中 0 行，抛
ConcurrentModificationError，从根上杜绝「丢失更新」。

使用两个独立 AsyncSession 模拟并发请求各自持有事务，逼近真实并发语义。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository._versioning import ConcurrentModificationError
from services.project_planning.repository.script_sqlalchemy_repo import (
    SqlAlchemyScriptRepository,
)
from tests.conftest import make_test_token

TEST_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture
async def client() -> AsyncClient:
    """带认证的 client（身份从 JWT 解析，走真实鉴权路径）。"""
    transport = ASGITransport(app=app)
    token = make_test_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c


async def _setup_project_and_episode(
    client: AsyncClient, *, project_id: str, episode_ref: str
) -> None:
    await client.post(
        "/projects",
        json={
            "project_name": f"项目_{project_id}",
            "project_tier": "B",
            "entity_id": project_id,
        },
    )
    await client.post(
        "/episodes",
        json={
            "project_id": project_id,
            "episode_ref": episode_ref,
            "title": f"集数_{episode_ref}",
        },
    )


async def _set_rls_context(session: object) -> None:
    """在直接操作仓储的会话里注入 RLS 上下文（app_role 下 RLS 生效）。"""
    from sqlalchemy import text

    await session.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": str(TEST_TENANT)},
    )


async def test_optimistic_lock_prevents_lost_update(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """两个并发会话基于同一旧版本推进，第二个必须抛并发冲突。

    模拟真实并发：会话 A 与 B 各自在独立事务中读到 entity_version=1 的同一
    DRAFT 剧本，各自迁移（version→2）。会话 A 先 commit，会话 B 的条件更新
    因 version 已非 1 而命中 0 行，抛冲突。
    """
    project_id = f"proj_ol_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_ref="EP001")

    # 导入一个 DRAFT 剧本（entity_version=1）。
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "并发锁定剧本",
            "entity_id": f"scr_ol_{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201
    script_id = resp.json()["entity_id"]

    # 会话 A：读取 -> 迁移（DRAFT→LOCK_PENDING）-> 保存（version 1 -> 2）。
    async with db_sessionmaker() as session_a:
        await _set_rls_context(session_a)
        repo_a = SqlAlchemyScriptRepository(session_a)
        script_a = await repo_a.get_by_id(script_id)
        assert script_a is not None
        expected_a = script_a.entity_version
        script_a.request_lock("writer_a")
        await repo_a.save(script_a, expected_a)
        await session_a.commit()

    # 会话 B：读取到已被 A 推进的版本（version=2），基于陈旧版本号 save
    # 必须抛 ConcurrentModificationError。
    async with db_sessionmaker() as session_b:
        await _set_rls_context(session_b)
        repo_b = SqlAlchemyScriptRepository(session_b)
        script_b = await repo_b.get_by_id(script_id)
        assert script_b is not None
        assert script_b.entity_version == 2  # 已被会话 A 推进

        stale = ScriptRevision(
            entity_id=script_id,
            project_id=script_b.project_id,
            episode_ref=script_b.episode_ref,
            title=script_b.title,
            created_by=script_b.created_by,
            tenant_id=TEST_TENANT,
            status=script_b.status,
            entity_version=1,  # 陈旧版本号，模拟并发读取旧快照
        )
        with pytest.raises(ConcurrentModificationError):
            await repo_b.save(stale, expected_version=1)
        await session_b.rollback()

    # 最终落库状态：仍为 LOCK_PENDING 且 version=2（未被陈旧写覆盖）。
    fetched = await client.get(f"/scripts/{script_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "LOCK_PENDING"
    assert fetched.json()["entity_version"] == 2
