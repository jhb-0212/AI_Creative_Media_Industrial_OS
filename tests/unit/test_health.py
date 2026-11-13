"""apps/api 健康检查与错误码端点的单元测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "api"
    assert body["version"]


def test_error_catalog_returns_known_code() -> None:
    resp = client.get("/errors/AUTH_SCOPE_DENIED")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "AUTH_SCOPE_DENIED"
    assert body["category"] == "PERMISSION"
    assert body["http_status"] == 403


def test_error_catalog_unknown_code_returns_404() -> None:
    resp = client.get("/errors/UNKNOWN_CODE")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
