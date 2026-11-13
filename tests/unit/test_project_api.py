"""Project API 集成测试（薄层路由 + 服务 + 内存仓储）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_create_project_success() -> None:
    resp = client.post(
        "/projects",
        json={"project_name": "测试项目", "project_tier": "A", "created_by": "user_001"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["project_name"] == "测试项目"
    assert body["project_tier"] == "A"
    assert body["entity_id"]


def test_create_project_idempotent() -> None:
    """幂等：同一 entity_id 重复创建返回同一项目，不产生第二条（D3 精神）。"""
    payload = {
        "project_name": "幂等项目",
        "project_tier": "B",
        "created_by": "user_002",
        "entity_id": "proj_idem_001",
    }
    r1 = client.post("/projects", json=payload)
    r2 = client.post("/projects", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["entity_id"] == r2.json()["entity_id"] == "proj_idem_001"


def test_create_project_rejects_empty_name() -> None:
    resp = client.post(
        "/projects",
        json={"project_name": "  ", "project_tier": "B", "created_by": "user_003"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_get_project_not_found() -> None:
    resp = client.get("/projects/does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"
