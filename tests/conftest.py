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
    create_budget_quota_engine,
    create_outbox_ops_engine,
    create_production_planning_engine,
    create_project_planning_engine,
    create_qc_governance_engine,
    create_sessionmaker,
)
from services.identity_issuance import issue_token  # noqa: E402

# 测试固定租户（历史数据已回填到此默认租户；新测试数据也用此租户，
# 保证 RLS 上下文一致，避免跨租户隔离误伤）。
TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


async def set_test_scope(
    executor,
    organization_id: uuid.UUID | None = None,
    *,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
) -> None:
    """把测试会话切到与生产同款的作用域（租户 + 组织）。

    为什么必须有这个 helper（ADR-R24 之后的新约束）：RLS 现在是双维度。只注入
    租户的会话会退化成「只看得到个人租户的行」——用它播种组织数据既插不进去
    （WITH CHECK 不过）也查不出来，而报错信息看起来像「权限不足」，很容易被
    理解成策略太严、然后去放宽策略。

    实现直接调生产函数 ``set_session_scope``，不在这里重写一份 SQL：测试要验证的
    正是「生产路径注入的那对上下文对不对」，自己拼一份就等于测了个平行实现。
    """
    from packages.shared.rls import set_session_scope

    await set_session_scope(
        executor, tenant_id=tenant_id, organization_id=organization_id
    )


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


@pytest.fixture(scope="session")
async def budget_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """budget_quota_role 连接（预算域运行时的真实连接角色）。

    该角色对 ``budget_quota.budget_ledger`` 只有 SELECT/INSERT：账本不可变是
    **权限形状**保证的，不是代码自觉。验证账本库层不变量（结算守卫）必须用它，
    否则「用超级用户证明了约束成立」并不能说明业务角色真的会撞上它。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.database import build_budget_quota_database_url

    engine = create_async_engine(build_budget_quota_database_url())
    sessionmaker = create_sessionmaker(engine)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
async def admin_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """超级用户连接（**唯一用途**：在 RLS 不生效的连接上验证约束本身）。

    为什么测试里需要一条超级用户连接（ADR-R24 §5 判据）：
    组织维度 RLS 生效后，子表策略用 ``EXISTS (父表)`` 表达归属，于是「父实体不存在」
    与「父实体不在当前作用域」变成同一个谓词，写入**先被 RLS 拒绝**（42501），
    外键（23503）永远轮不到。若只用业务连接做负向测试，就再也无法证明
    「外键真的在数据库里独立生效」——而那正是不变量 SCOPE-2/SCOPE-3 的内容。

    所以这里换一条**不做行级安全**的连接跑同一句 INSERT：外键若真的存在且有效，
    它就必须拒绝。两层各自独立成立，才是「两层防线」而不是「一层防线两处描述」。

    前提必须被验证而不是假设：fixture 先断言当前角色确实是超级用户（否则
    「绕过 RLS」这个前提不成立，用例会变成一个静默的假阳性）。
    **禁止**用它验证任何授权逻辑——那会绕过被测对象本身。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.database import build_migration_url

    engine = create_async_engine(build_migration_url())
    sessionmaker = create_sessionmaker(engine)
    try:
        async with engine.connect() as conn:
            is_super = await conn.scalar(
                text(
                    "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                )
            )
            assert is_super, (
                "admin_sessionmaker 必须以超级用户连接（否则无法绕过 RLS，"
                "本 fixture 的存在前提不成立）"
            )
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
    production_engine = create_production_planning_engine()
    budget_engine = create_budget_quota_engine()
    qc_engine = create_qc_governance_engine()
    project_sessionmaker = create_sessionmaker(project_engine)
    asset_sessionmaker = create_sessionmaker(asset_engine)
    app.state.project_planning_engine = project_engine
    app.state.asset_service_engine = asset_engine
    app.state.production_planning_engine = production_engine
    app.state.budget_quota_engine = budget_engine
    app.state.qc_governance_engine = qc_engine
    app.state.project_planning_sessionmaker = project_sessionmaker
    app.state.asset_service_sessionmaker = asset_sessionmaker
    # R16 三域（a0b1c2d3e4f5）：路由按域注入，测试装配必须提供同一批池，
    # 否则「装配缺失」会在第一个用到该域的路由请求上变成 500。
    app.state.production_planning_sessionmaker = create_sessionmaker(production_engine)
    app.state.budget_quota_sessionmaker = create_sessionmaker(budget_engine)
    app.state.qc_governance_sessionmaker = create_sessionmaker(qc_engine)
    # D6-9 运维重放池：路由注入的是 outbox_ops_sessionmaker，缺它会在第一个
    # 重放请求上变成 AttributeError（装配缺失伪装成 500）。
    ops_engine = create_outbox_ops_engine()
    app.state.outbox_ops_engine = ops_engine
    app.state.outbox_ops_sessionmaker = create_sessionmaker(ops_engine)
    # 向后兼容别名：/readyz 与既有代码读 app.state.sessionmaker 时，落到
    # project_planning 域会话工厂（与生产 lifespan 的别名语义一致）。
    app.state.engine = project_engine
    app.state.sessionmaker = project_sessionmaker
    try:
        yield project_sessionmaker
    finally:
        for engine in (
            project_engine,
            asset_engine,
            production_engine,
            budget_engine,
            qc_engine,
            ops_engine,
        ):
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
