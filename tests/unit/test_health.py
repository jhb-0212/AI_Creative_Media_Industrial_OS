"""apps/api 健康检查与错误码端点的单元测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app


@pytest.fixture
def client() -> TestClient:
    """用 with 触发 lifespan（启动/关闭路径与生产一致）。

    lifespan 会重新装配 app.state 的引擎/会话工厂；退出后恢复 conftest 注入的
    测试引擎，避免污染同会话后续用例。
    """
    previous_engine = getattr(app.state, "engine", None)
    previous_sessionmaker = getattr(app.state, "sessionmaker", None)
    with TestClient(app) as c:
        yield c
    app.state.engine = previous_engine
    app.state.sessionmaker = previous_sessionmaker


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "api"
    assert body["version"]


def test_error_catalog_returns_known_code(client: TestClient) -> None:
    resp = client.get("/errors/AUTH_SCOPE_DENIED")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "AUTH_SCOPE_DENIED"
    assert body["category"] == "PERMISSION"
    assert body["http_status"] == 403


def test_error_catalog_unknown_code_returns_404(client: TestClient) -> None:
    resp = client.get("/errors/UNKNOWN_CODE")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"
