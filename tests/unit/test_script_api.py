"""ScriptRevision API 集成测试（异步，连真实 PostgreSQL）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 测试数据使用唯一前缀，避免相互污染。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_import_script_success(client: AsyncClient) -> None:
    resp = await client.post(
        "/scripts/import",
        json={
            "project_id": f"proj_{uuid.uuid4().hex[:8]}",
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
    eid = f"scr_idem_{uuid.uuid4().hex[:8]}"
    payload = {
        "project_id": "proj_idem",
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


async def test_import_script_supersedes_old(client: AsyncClient) -> None:
    """新版本导入使旧版本转 SUPERSEDED（终态）。"""
    project_id = f"proj_sup_{uuid.uuid4().hex[:8]}"
    # 第一个版本
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

    # 第二个版本
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

    # 旧版本应转 SUPERSEDED
    fetched_old = await client.get(f"/scripts/{first_id}")
    assert fetched_old.status_code == 200
    assert fetched_old.json()["status"] == "SUPERSEDED"

    # 新版本仍为 DRAFT
    fetched_new = await client.get(f"/scripts/{second_id}")
    assert fetched_new.json()["status"] == "DRAFT"


async def test_get_script_not_found(client: AsyncClient) -> None:
    resp = await client.get(f"/scripts/not_exist_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_get_script_roundtrip(client: AsyncClient) -> None:
    """导入后查询，验证真实落库。"""
    eid = f"scr_rt_{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/scripts/import",
        json={
            "project_id": "proj_rt",
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
