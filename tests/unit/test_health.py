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


def test_readyz_reports_database_up(client: TestClient) -> None:
    """就绪探针必须真实打库：库可用时 200 且显式给出双域池 database=up。"""
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    # 连接池按域拆分后就绪必须覆盖两个池（Codex P2）：只探 project 池时，
    # asset 凭据/角色损坏仍会回报 ready，资产路由流量进来变成 500。
    assert body["checks"]["project_planning_database"] == "up"
    assert body["checks"]["asset_service_database"] == "up"


def test_readyz_reports_schema_revision(client: TestClient) -> None:
    """就绪面回报 alembic revision：用于识别"连到了另一个库实例"（ADR-R10）。"""
    body = client.get("/readyz").json()
    assert isinstance(body["schema_revision"], str)
    assert body["schema_revision"]


def _broken_sessionmaker() -> None:
    raise RuntimeError("database unreachable")


def test_readyz_fails_closed_when_project_pool_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project 池不可用必须 503（fail-closed），不能把流量放进来变成 500。"""
    monkeypatch.setattr(app.state, "project_planning_sessionmaker", _broken_sessionmaker)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unready"
    assert body["checks"]["project_planning_database"] == "down"


def test_readyz_fails_closed_when_asset_pool_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仅 asset 池损坏也必须 503（Codex P2）：单池探针会漏报这种半故障。"""
    monkeypatch.setattr(app.state, "asset_service_sessionmaker", _broken_sessionmaker)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unready"
    assert body["checks"]["asset_service_database"] == "down"


def test_error_catalog_closed_by_default(client: TestClient) -> None:
    """默认关闭：即使非生产环境也不暴露错误码目录，且与未知码不可区分。"""
    resp = client.get("/errors/AUTH_SCOPE_DENIED")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


@pytest.fixture
def catalog_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """把错误码目录显式打开（模拟 ERRORS_CATALOG_PUBLIC=true）。"""
    import apps.api.main as main_module

    monkeypatch.setattr(
        main_module,
        "settings",
        main_module.settings.model_copy(update={"errors_catalog_public": True}),
    )


def test_error_catalog_returns_known_code(client: TestClient, catalog_open: None) -> None:
    resp = client.get("/errors/AUTH_SCOPE_DENIED")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "AUTH_SCOPE_DENIED"
    assert body["category"] == "PERMISSION"
    assert body["http_status"] == 403


def test_error_catalog_unknown_code_returns_404(client: TestClient, catalog_open: None) -> None:
    resp = client.get("/errors/UNKNOWN_CODE")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


def test_error_catalog_requires_matching_internal_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭状态下带令牌：头缺失或不等一律 404；完全一致才放行。"""
    from pydantic import SecretStr

    import apps.api.main as main_module

    monkeypatch.setattr(
        main_module,
        "settings",
        main_module.settings.model_copy(
            update={
                "errors_catalog_public": False,
                "errors_catalog_token": SecretStr("internal-secret"),
            }
        ),
    )

    assert client.get("/errors/AUTH_SCOPE_DENIED").status_code == 404
    wrong = client.get("/errors/AUTH_SCOPE_DENIED", headers={"X-Internal-Token": "guess"})
    assert wrong.status_code == 404
    ok = client.get("/errors/AUTH_SCOPE_DENIED", headers={"X-Internal-Token": "internal-secret"})
    assert ok.status_code == 200
    assert ok.json()["code"] == "AUTH_SCOPE_DENIED"
