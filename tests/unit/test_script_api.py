"""ScriptRevision API 集成测试（异步，连真实 PostgreSQL）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 测试数据使用唯一前缀，避免相互污染。
- 归属验证是 SCRIPT.IMPORT 的前置：每个用例先建立「project + episode」合法归属，
  再导入剧本。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _setup_project_and_episode(
    client: AsyncClient,
    *,
    project_id: str,
    episode_refs: list[str],
) -> None:
    """建立「项目 + 集数」合法归属，供剧本导入用例前置调用。"""
    await client.post(
        "/projects",
        json={
            "project_name": f"项目_{project_id}",
            "project_tier": "B",
            "created_by": "user_owner",
            "entity_id": project_id,
        },
    )
    for ref in episode_refs:
        await client.post(
            "/episodes",
            json={
                "project_id": project_id,
                "episode_ref": ref,
                "title": f"集数_{ref}",
                "created_by": "user_owner",
            },
        )


async def test_import_script_success(client: AsyncClient) -> None:
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": f"剧本_{uuid.uuid4().hex[:8]}",
            "created_by": "user_script",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["entity_type"] == "ScriptRevision"
    assert body["entity_id"]


async def test_import_script_idempotent(client: AsyncClient) -> None:
    """幂等：同一 entity_id 重复导入返回同一版本。"""
    project_id = f"proj_idem_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    eid = f"scr_idem_{uuid.uuid4().hex[:8]}"
    payload = {
        "project_id": project_id,
        "episode_ref": "EP001",
        "title": "幂等剧本",
        "created_by": "user_script",
        "entity_id": eid,
    }
    r1 = await client.post("/scripts/import", json=payload)
    r2 = await client.post("/scripts/import", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["entity_id"] == r2.json()["entity_id"] == eid
    assert r1.json()["entity_version"] == r2.json()["entity_version"] == 1


async def test_import_script_rejects_empty_title(client: AsyncClient) -> None:
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": "proj_x",
            "episode_ref": "EP001",
            "title": "  ",
            "created_by": "user_script",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_import_script_rejects_missing_project(client: AsyncClient) -> None:
    """归属验证：project 不存在时拒绝（NOT_FOUND）。"""
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": f"proj_missing_{uuid.uuid4().hex[:8]}",
            "episode_ref": "EP001",
            "title": "标题",
            "created_by": "user_script",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_import_script_rejects_episode_not_owned(client: AsyncClient) -> None:
    """归属验证：episode 不属于该项目时拒绝（VALIDATION_ERROR）。"""
    project_id = f"proj_own_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    # EP002 未登记为该项目集数，导入应被拒绝。
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP002",
            "title": "标题",
            "created_by": "user_script",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_import_script_supersedes_old(client: AsyncClient) -> None:
    """新版本导入使旧版本转 SUPERSEDED（终态）。"""
    project_id = f"proj_sup_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    r1 = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "第一版剧本",
            "created_by": "user_script",
        },
    )
    assert r1.status_code == 201
    first_id = r1.json()["entity_id"]

    r2 = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "第二版剧本",
            "created_by": "user_script",
        },
    )
    assert r2.status_code == 201
    second_id = r2.json()["entity_id"]
    assert second_id != first_id

    fetched_old = await client.get(f"/scripts/{first_id}")
    assert fetched_old.status_code == 200
    assert fetched_old.json()["status"] == "SUPERSEDED"

    fetched_new = await client.get(f"/scripts/{second_id}")
    assert fetched_new.json()["status"] == "DRAFT"


async def test_import_script_isolates_episodes(client: AsyncClient) -> None:
    """版本链按集数隔离：导入 EP002 不得将 EP001 标为 SUPERSEDED（P1 跨集误替换回归）。"""
    project_id = f"proj_epi_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(
        client, project_id=project_id, episode_refs=["EP001", "EP002", "EP003"]
    )

    r1 = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "EP001 第一版",
            "created_by": "user_script",
        },
    )
    assert r1.status_code == 201
    ep1_id = r1.json()["entity_id"]

    r2 = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP002",
            "title": "EP002 第一版",
            "created_by": "user_script",
        },
    )
    assert r2.status_code == 201

    fetched_ep1 = await client.get(f"/scripts/{ep1_id}")
    assert fetched_ep1.status_code == 200
    assert fetched_ep1.json()["status"] == "DRAFT"

    r3 = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP003",
            "title": "EP003 第一版",
            "created_by": "user_script",
        },
    )
    assert r3.status_code == 201

    fetched_ep2 = await client.get(f"/scripts/{r2.json()['entity_id']}")
    assert fetched_ep2.status_code == 200
    assert fetched_ep2.json()["status"] == "DRAFT"


async def test_get_script_not_found(client: AsyncClient) -> None:
    resp = await client.get(f"/scripts/not_exist_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_get_script_roundtrip(client: AsyncClient) -> None:
    """导入后查询，验证真实落库。"""
    project_id = f"proj_rt_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP002"])

    eid = f"scr_rt_{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP002",
            "title": "落库验证剧本",
            "created_by": "user_script",
            "entity_id": eid,
        },
    )
    assert created.status_code == 201

    fetched = await client.get(f"/scripts/{eid}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["entity_id"] == eid
    assert body["title"] == "落库验证剧本"
    assert body["episode_ref"] == "EP002"
    assert body["status"] == "DRAFT"


async def test_lock_script_success(client: AsyncClient) -> None:
    """SCRIPT.LOCK.APPROVE：DRAFT→LOCKED，申请人与批准人分离。"""
    project_id = f"proj_lock_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    r = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
            "created_by": "user_script",
        },
    )
    assert r.status_code == 201
    script_id = r.json()["entity_id"]

    lock = await client.post(
        f"/scripts/{script_id}/lock",
        json={"requested_by": "编剧A", "approved_by": "导演B"},
    )
    assert lock.status_code == 200
    assert lock.json()["status"] == "LOCKED"
    assert lock.json()["entity_version"] == 2  # DRAFT(1) -> LOCKED(2)


async def test_lock_script_rejects_self_approval(client: AsyncClient) -> None:
    """红线：申请人==批准人（自锁）拒绝。"""
    project_id = f"proj_self_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    r = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
            "created_by": "user_script",
        },
    )
    script_id = r.json()["entity_id"]

    lock = await client.post(
        f"/scripts/{script_id}/lock",
        json={"requested_by": "同一人", "approved_by": "同一人"},
    )
    assert lock.status_code == 409
    assert lock.json()["error_code"] == "STATE_TRANSITION_DENIED"


async def test_lock_script_rejects_non_draft(client: AsyncClient) -> None:
    """已锁定态再次锁定拒绝（状态机：LOCKED 无后继转移）。"""
    project_id = f"proj_relock_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(client, project_id=project_id, episode_refs=["EP001"])

    r = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
            "created_by": "user_script",
        },
    )
    script_id = r.json()["entity_id"]

    # 第一次锁定成功
    first = await client.post(
        f"/scripts/{script_id}/lock",
        json={"requested_by": "编剧A", "approved_by": "导演B"},
    )
    assert first.status_code == 200

    # 第二次锁定（已 LOCKED）拒绝
    second = await client.post(
        f"/scripts/{script_id}/lock",
        json={"requested_by": "编剧A", "approved_by": "导演C"},
    )
    assert second.status_code == 409
    assert second.json()["error_code"] == "STATE_TRANSITION_DENIED"
