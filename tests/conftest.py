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

# --------------------------------------------------------------------------- #
# 测试密钥引导（必须在任何 app/deps 导入之前执行，KEYS-1）
# --------------------------------------------------------------------------- #
# 验签方 get_public_key_store() 只从 IDENTITY_PUBLIC_KEY_PEM 取公钥（不再从签发
# 私钥派生）。测试进程内「签发 token」与「API 验签」必须同源——此处引导一对
# 显式注入的密钥，让签发方（identity 角色读 IDENTITY_PRIVATE_KEY_PEM +
# IDENTITY_PUBLIC_KEY_PEM）与验签方（读 IDENTITY_PUBLIC_KEY_PEM）共享同一对密钥。
from packages.identity.bootstrap import bootstrap_signing_key_if_missing

bootstrap_signing_key_if_missing()

# 以下 import 必须在密钥引导之后（否则验签方缺公钥拒绝启动），故 E402 豁免。
from apps.api.main import app  # noqa: E402
from packages.shared.database import (  # noqa: E402
    build_relay_database_url,
    create_asset_service_engine,
    create_project_planning_engine,
    create_sessionmaker,
)
from services.identity_issuance import issue_token  # noqa: E402

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
    capabilities: list[str] | None = None,
    organization_id: uuid.UUID | None = None,
) -> str:
    """签发一条测试访问令牌（身份服务内部签发，含私钥）。

    默认主体 user_script（编剧），角色可覆盖；tenant_id 默认测试租户。
    capabilities 默认签发全通配 ``*``（测试 token 视为「受信任的完整授权
    主体」），使既有正向用例专注验证「角色授权」而不被能力校验干扰；
    负向用例可显式传入空列表或受限能力集来验证「缺能力拒绝」。
    organization_id：组织归属（可选，org claim）——组织项目的资源级 ABAC
    负向用例据此构造「他组织成员」的令牌。

    测试据此构造认证头，走真实验签路径（R6/R9）。
    """
    return issue_token(
        subject_id=subject_id,
        tenant_id=str(tenant_id),
        roles=roles or ["导演", "编剧", "制片"],
        capabilities=capabilities if capabilities is not None else ["*"],
        organization_id=str(organization_id) if organization_id else None,
    )


@pytest.fixture(scope="session")
async def relay_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """outbox_relay_role 连接（Relay 运行时的真实连接角色，F03 进程边界隔离）。

    Relay 只消费 outbox 事件并回写投递状态，以专用角色连接；该角色仅持有
    outbox schema 的 SELECT + UPDATE，不持有业务 schema 写权限，也不持有
    outbox 的 INSERT（事件只能由业务域同事务写入）。测试据此验证「relay 越界
    写业务表 / 伪造事件」在数据库层被拒绝，而非依赖代码自觉。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(build_relay_database_url())
    sessionmaker = create_sessionmaker(engine)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
async def asset_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """asset_service_role 连接（asset_service 域运行时的真实连接角色）。

    DATA-1 拆角色后，asset_service 域的表（asset_versions / qc_reports）只能
    由 asset_service_role 读写；project_planning_role（db_sessionmaker）对
    asset_service schema 无 USAGE 权限。供资产域集成测试做「持久化核对」——
    直接读 asset_service 表必须用 asset_service 域角色，而非退回 app_role。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.database import build_asset_service_database_url

    engine = create_async_engine(build_asset_service_database_url())
    sessionmaker = create_sessionmaker(engine)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """为整个测试会话装配按域引擎 + 会话工厂，并挂到 app.state（P0-4 + DATA-1）。

    ASGITransport 不触发 lifespan，因此生产代码的 lifespan 不会在测试里执行；
    这里由 conftest 注入按域独立引擎，保证 app.state 各域会话工厂在测试期间
    可用，同时与生产引擎彻底隔离（ADR 3.4：测试用独立引擎，生产代码零测试感知）。

    DATA-1（按域拆连接池）：生产按 project_planning / asset_service 两个域挂
    独立会话工厂（database.lifespan），各自以 project_planning_role /
    asset_service_role 连接（app_role 对业务 schema 的权限已被迁移
    8f3b2c1d4e5a 回收）。测试装配必须与生产契约一致，使用**真实域角色连接**，
    而非退回 app_role——否则认证路径读 auth.revoked_tokens（吊销检查）与业务
    读写都会因 app_role 权限被回收而失败，且这种失败恰是 DATA-1 隔离生效的
    证据，不能用「测试退回 app_role」掩盖。

    无真实 PG 时连接为惰性（单元测试不触发连接）；真实角色隔离与权限口径
    由 tests/integration（需真实 PG）验证，此处只负责按域挂对会话工厂。
    """
    project_engine = create_project_planning_engine()
    asset_engine = create_asset_service_engine()
    project_sessionmaker = create_sessionmaker(project_engine)
    asset_sessionmaker = create_sessionmaker(asset_engine)
    app.state.project_planning_engine = project_engine
    app.state.asset_service_engine = asset_engine
    app.state.project_planning_sessionmaker = project_sessionmaker
    app.state.asset_service_sessionmaker = asset_sessionmaker
    # 向后兼容别名：/readyz 与既有代码读 app.state.sessionmaker 时，落到
    # project_planning 域会话工厂（与生产 lifespan 的别名语义一致）。
    app.state.engine = project_engine
    app.state.sessionmaker = project_sessionmaker
    try:
        yield project_sessionmaker
    finally:
        await project_engine.dispose()
        await asset_engine.dispose()


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
