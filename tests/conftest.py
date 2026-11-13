"""pytest 全局配置。

策略：
- 领域模型单元测试：纯内存，无外部依赖。
- API 集成测试：依赖真实 PostgreSQL（docker-compose 已起，符合 A1 可复现）。
- 身份治理（P0-3）：API 集成测试走真实鉴权——先签发测试 token，请求带
  ``Authorization: Bearer <token>``，身份从 JWT 解析（非客户端字符串）。
- 事件循环：由 pytest-asyncio 的 session 级 loop 管理（见 pyproject.toml 的
  `asyncio_default_fixture_loop_scope` / `asyncio_default_test_loop_scope`），
  确保 asyncpg 连接池跨用例复用同一事件循环，避免跨循环失效。
  不再自定义旧版 `event_loop` fixture（pytest-asyncio>=0.23 已弃用该写法）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.shared.database import create_engine, create_sessionmaker
from services.identity_access import issue_token

# 测试固定租户（历史数据已回填到此默认租户；新测试数据也用此租户，
# 保证 RLS 上下文一致，避免跨租户隔离误伤）。
TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def make_test_token(
    subject_id: str = "user_script",
    roles: list[str] | None = None,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
) -> str:
    """签发一条测试访问令牌（身份服务内部签发，含私钥）。

    默认主体 user_script（编剧），角色可覆盖；tenant_id 默认测试租户。
    测试据此构造认证头，走真实验签路径（R6/R9）。
    """
    return issue_token(
        subject_id=subject_id,
        tenant_id=str(tenant_id),
        roles=roles or ["导演", "编剧", "制片"],
    )


@pytest.fixture(scope="session", autouse=True)
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """为整个测试会话装配独立引擎 + 会话工厂，并挂到 app.state（P0-4 根治）。

    ASGITransport 不触发 lifespan，因此生产代码的 lifespan 不会在测试里执行；
    这里由 conftest 注入独立引擎，保证 app.state.sessionmaker 在测试期间可用，
    同时与生产引擎彻底隔离（ADR 3.4：测试用独立引擎，生产代码零测试感知）。
    """
    engine = create_engine()
    sessionmaker = create_sessionmaker(engine)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
async def client() -> AsyncClient:
    """无认证的裸 client（用于测 401 负向场景）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_client() -> AsyncClient:
    """带认证头的 client（默认主体 user_script，测试租户）。

    所有走业务 API 的测试应用此 fixture，使请求带有效 Bearer token，
    身份与 tenant 上下文从 JWT 解析（真实鉴权路径）。
    """
    transport = ASGITransport(app=app)
    token = make_test_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c
