"""Project API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 身份治理（P0-3）：请求带 Authorization Bearer，created_by / tenant_id 从 JWT
  解析，非客户端字符串；测试数据用唯一前缀避免相互污染。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def test_create_project_success(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/projects",
        json={
            "project_name": f"测试项目_{uuid.uuid4().hex[:8]}",
            "project_tier": "A",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["project_tier"] == "A"
    assert body["entity_id"]
    assert body["created_by"] == "user_script"  # 身份来自 JWT，非请求体
    assert body["tenant_id"]


async def test_create_project_idempotent(auth_client: AsyncClient) -> None:
    """幂等：同一 entity_id 重复创建返回同一项目，不产生第二条（D3 精神）。"""
    eid = f"proj_idem_{uuid.uuid4().hex[:8]}"
    payload = {
        "project_name": "幂等项目",
        "project_tier": "B",
        "entity_id": eid,
    }
    r1 = await auth_client.post("/projects", json=payload)
    r2 = await auth_client.post("/projects", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["entity_id"] == r2.json()["entity_id"] == eid
    assert r1.json()["entity_version"] == r2.json()["entity_version"] == 1


async def test_create_project_rejects_empty_name(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/projects",
        json={"project_name": "  ", "project_tier": "B"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_create_project_rejects_unauthenticated(client: AsyncClient) -> None:
    """无认证请求被拒绝（401）——身份治理红线，安全必须真实执行。"""
    resp = await client.post(
        "/projects",
        json={"project_name": "未认证项目", "project_tier": "B"},
    )
    assert resp.status_code == 401


async def test_get_project_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.get(f"/projects/not_exist_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_get_project_roundtrip(auth_client: AsyncClient) -> None:
    """创建后查询，验证真实落库（读写闭环）。"""
    eid = f"proj_rt_{uuid.uuid4().hex[:8]}"
    created = await auth_client.post(
        "/projects",
        json={
            "project_name": "落库验证项目",
            "project_tier": "S",
            "entity_id": eid,
        },
    )
    assert created.status_code == 201

    fetched = await auth_client.get(f"/projects/{eid}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["entity_id"] == eid
    assert body["project_name"] == "落库验证项目"
    assert body["project_tier"] == "S"
