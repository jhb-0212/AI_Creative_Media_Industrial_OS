"""ScriptRevision API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 身份治理（P0-3）：请求带 Authorization Bearer，身份从 JWT 解析；两步审批流
  （request_lock 申请人 → approve_lock 批准人），职责分离是运行时真实主体比对。
- 测试数据使用唯一前缀，避免相互污染。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import make_test_token


async def _auth_headers(subject_id: str) -> dict[str, str]:
    """构造指定主体的认证头（模拟不同真实主体）。"""
    return {"Authorization": f"Bearer {make_test_token(subject_id=subject_id)}"}


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
            },
        )


async def test_import_script_success(auth_client: AsyncClient) -> None:
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    resp = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": f"剧本_{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["entity_type"] == "ScriptRevision"
    assert body["entity_id"]
    assert body["created_by"] == "user_script"  # 身份来自 JWT


async def test_import_script_idempotent(auth_client: AsyncClient) -> None:
    """幂等：同一 entity_id 重复导入返回同一版本。"""
    project_id = f"proj_idem_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    eid = f"scr_idem_{uuid.uuid4().hex[:8]}"
    payload = {
        "project_id": project_id,
        "episode_ref": "EP001",
        "title": "幂等剧本",
        "entity_id": eid,
    }
    r1 = await auth_client.post("/scripts/import", json=payload)
    r2 = await auth_client.post("/scripts/import", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["entity_id"] == r2.json()["entity_id"] == eid
    assert r1.json()["entity_version"] == r2.json()["entity_version"] == 1


async def test_import_script_rejects_empty_title(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": "proj_x",
            "episode_ref": "EP001",
            "title": "  ",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_import_script_rejects_missing_project(auth_client: AsyncClient) -> None:
    """归属验证：project 不存在时拒绝（NOT_FOUND）。"""
    resp = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": f"proj_missing_{uuid.uuid4().hex[:8]}",
            "episode_ref": "EP001",
            "title": "标题",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_import_script_rejects_episode_not_owned(auth_client: AsyncClient) -> None:
    """归属验证：episode 不属于该项目时拒绝（VALIDATION_ERROR）。"""
    project_id = f"proj_own_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    resp = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP002",
            "title": "标题",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_import_script_supersedes_old(auth_client: AsyncClient) -> None:
    """新版本导入使旧版本转 SUPERSEDED（终态）。"""
    project_id = f"proj_sup_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    r1 = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "第一版剧本",
        },
    )
    assert r1.status_code == 201
    first_id = r1.json()["entity_id"]

    r2 = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "第二版剧本",
        },
    )
    assert r2.status_code == 201
    second_id = r2.json()["entity_id"]
    assert second_id != first_id

    fetched_old = await auth_client.get(f"/scripts/{first_id}")
    assert fetched_old.status_code == 200
    assert fetched_old.json()["status"] == "SUPERSEDED"

    fetched_new = await auth_client.get(f"/scripts/{second_id}")
    assert fetched_new.json()["status"] == "DRAFT"


async def test_get_script_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.get(f"/scripts/not_exist_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_get_script_roundtrip(auth_client: AsyncClient) -> None:
    """导入后查询，验证真实落库。"""
    project_id = f"proj_rt_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP002"])

    eid = f"scr_rt_{uuid.uuid4().hex[:8]}"
    created = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP002",
            "title": "落库验证剧本",
            "entity_id": eid,
        },
    )
    assert created.status_code == 201

    fetched = await auth_client.get(f"/scripts/{eid}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["entity_id"] == eid
    assert body["title"] == "落库验证剧本"
    assert body["episode_ref"] == "EP002"
    assert body["status"] == "DRAFT"


async def test_lock_two_step_flow(auth_client: AsyncClient) -> None:
    """两步审批流：申请人 request_lock → 批准人 approve_lock（职责分离）。"""
    project_id = f"proj_lock_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    # 编剧导入剧本
    writer_headers = await _auth_headers("writer_001")
    r = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
        },
    )
    assert r.status_code == 201
    script_id = r.json()["entity_id"]

    # 第一步：申请人（编剧）提交锁定申请
    req = await auth_client.post(
        f"/scripts/{script_id}/lock/request",
        headers=writer_headers,
    )
    assert req.status_code == 200
    assert req.json()["status"] == "LOCK_PENDING"
    assert req.json()["requested_by"] == "writer_001"

    # 第二步：批准人（导演）批准
    director_headers = await _auth_headers("director_001")
    appr = await auth_client.post(
        f"/scripts/{script_id}/lock/approve",
        headers=director_headers,
    )
    assert appr.status_code == 200
    assert appr.json()["status"] == "LOCKED"


async def test_lock_rejects_self_approval(auth_client: AsyncClient) -> None:
    """红线：申请人自己批准（自锁）拒绝——运行时真实主体比对。"""
    project_id = f"proj_self_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    r = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
        },
    )
    script_id = r.json()["entity_id"]

    # 同一主体（writer_001）既申请又批准
    writer_headers = await _auth_headers("writer_001")
    req = await auth_client.post(
        f"/scripts/{script_id}/lock/request",
        headers=writer_headers,
    )
    assert req.status_code == 200

    appr = await auth_client.post(
        f"/scripts/{script_id}/lock/approve",
        headers=writer_headers,  # 同一人，应拒绝
    )
    assert appr.status_code == 409
    assert appr.json()["error_code"] == "STATE_TRANSITION_DENIED"


async def test_lock_rejects_non_pending(auth_client: AsyncClient) -> None:
    """非 LOCK_PENDING 态批准拒绝（状态机：DRAFT 无 approve 转移）。"""
    project_id = f"proj_relock_{uuid.uuid4().hex[:8]}"
    await _setup_project_and_episode(auth_client, project_id=project_id, episode_refs=["EP001"])

    r = await auth_client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "待锁定剧本",
        },
    )
    script_id = r.json()["entity_id"]

    # 未申请就直接批准（DRAFT 态），状态机拒绝
    director_headers = await _auth_headers("director_001")
    appr = await auth_client.post(
        f"/scripts/{script_id}/lock/approve",
        headers=director_headers,
    )
    assert appr.status_code == 409
    assert appr.json()["error_code"] == "STATE_TRANSITION_DENIED"
