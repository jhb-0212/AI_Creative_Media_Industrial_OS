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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.identity.action_registry import all_action_specs
from packages.identity.principal import Principal
from packages.shared.database import DomainPools
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import set_request_context
from services.identity_access import issue_capability, issue_token
from tests import TEST_SIGNING_KEY

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
    project_ids: list[str] | None = None,
    organization_id: uuid.UUID | str | None = None,
    subject_type: str = "human",
) -> str:
    """签发一条测试访问令牌（身份服务内部签发，含私钥）。

    默认主体 user_script（编剧），角色可覆盖；tenant_id 默认测试租户。

    ``project_ids`` 默认 ``["*"]``（租户内全部项目）：绝大多数用例要验证的是
    业务规则本身而不是范围限制，给全范围可避免每条用例重复声明项目。
    需要验证项目级隔离的用例显式传受限范围（如 ``["proj-a"]``）——这类用例在
    tests/unit/test_project_scope.py 里集中管理，因为「范围必须被真实执行」
    本身是要被证明的行为，不能靠默认值掩盖。
    """
    return issue_token(
        subject_id=subject_id,
        tenant_id=str(tenant_id),
        roles=roles or ["导演", "编剧", "制片"],
        subject_type=subject_type,
        organization_id=str(organization_id) if organization_id is not None else None,
        project_ids=project_ids if project_ids is not None else ["*"],
        # 显式传入测试私钥：签发能力不来自进程配置（B4）。
        signing_key=TEST_SIGNING_KEY,
    )


def make_test_capability_token(
    subject_id: str = "user_script",
    tenant_id: uuid.UUID = TEST_TENANT_ID,
    organization_id: uuid.UUID | str | None = None,
    allowed_actions: list[str] | None = None,
    project_scope: list[str] | None = None,
    **narrowing: object,
) -> str:
    """签发测试用短时 capability。

    默认允许全部已注册动作、项目范围 ``*``：这是测试夹具的便利默认值，
    不是生产默认值——生产签发端要求调用方显式给出收窄范围。
    需要验证越权时显式传 ``allowed_actions`` / ``project_scope``。
    """
    return issue_capability(
        subject_id=subject_id,
        tenant_id=str(tenant_id),
        organization_id=str(organization_id or tenant_id),
        allowed_actions=(
            list(allowed_actions)
            if allowed_actions is not None
            else [spec.action_key for spec in all_action_specs()]
        ),
        project_scope=project_scope if project_scope is not None else ["*"],
        task_id="test-task",
        workflow_run_id="test-run",
        signing_key=TEST_SIGNING_KEY,
        **narrowing,  # type: ignore[arg-type]
    )


def make_test_headers(
    subject_id: str = "user_script",
    roles: list[str] | None = None,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
    project_ids: list[str] | None = None,
    organization_id: uuid.UUID | str | None = None,
    subject_type: str = "human",
    allowed_actions: list[str] | None = None,
    project_scope: list[str] | None = None,
    **capability_narrowing: object,
) -> dict[str, str]:
    """同时构造访问令牌与 capability 头（测试入口统一走真实双凭证链路）。"""
    return {
        "Authorization": f"Bearer {make_test_token(subject_id, roles, tenant_id, project_ids, organization_id, subject_type)}",
        "X-Capability-Token": make_test_capability_token(
            subject_id,
            tenant_id,
            organization_id,
            allowed_actions,
            project_scope
            if project_scope is not None
            else (project_ids if project_ids is not None else ["*"]),
            **capability_narrowing,
        ),
    }


async def apply_db_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
    project_ids: tuple[str, ...] = ("*",),
) -> None:
    """在测试会话内注入与生产一致的 RLS 上下文（租户 + 项目范围）。

    直接复用生产注入函数，而不是在测试里另写一份裸 SQL：这样"生产如何设置
    上下文"与"测试如何设置上下文"不可能出现两种口径——两份口径下，测试会
    在错误的前提下全绿，比没有测试更危险。

    需要验证隔离行为的用例显式传窄范围（例如 ``project_ids=("proj-a",)``）。
    """
    await set_request_context(
        session,
        Principal(
            subject_id="probe",
            tenant_id=tenant_id,
            organization_id=tenant_id,
            project_ids=frozenset(project_ids),
        ),
    )


async def set_raw_rls_context(
    session: AsyncSession,
    *,
    tenant_id: str,
    projects: str = "*",
    organization_id: str | None = None,
) -> None:
    """以裸 SQL 注入 RLS 上下文（租户 + 组织 + 项目范围），不经过生产装配。

    与 ``apply_db_context`` 的分工：那个用生产函数，证明"生产路径正确"；
    这个刻意绕过装配层，用在**证明裸 SQL 绕不过 RLS** 的用例里——
    如果两者共用同一条代码路径，就变成"自己证明自己"。

    ``organization_id`` 缺省等于 tenant（单组织租户基线），与迁移回填口径一致；
    三个 GUC 缺任何一个，策略都会判否——这正是要证明的"未注入即看不见"。
    """
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"),
        {"org": organization_id or tenant_id},
    )
    await session.execute(
        text("SELECT set_config('app.current_projects', :projects, true)"),
        {"projects": projects},
    )


@pytest.fixture(scope="session", autouse=True)
async def db_pools() -> AsyncIterator[DomainPools]:
    """为整个测试会话装配按域隔离的连接池，并挂到 app.state（P0-4 根治）。

    ASGITransport 不触发 lifespan，因此生产代码的 lifespan 不会在测试里执行；
    这里由 conftest 注入独立 ``DomainPools``，保证 ``app.state.pools`` 在测试期间
    可用，同时与生产池彻底隔离（ADR 3.4：测试用独立引擎，生产代码零测试感知）。

    测试拿到的池**也是按域角色连接**的：测试里写错域会被数据库拒绝，
    这正是被测行为的一部分，不允许测试用超级用户绕过。
    """
    pools = DomainPools.create()
    app.state.pools = pools
    # 镜像生产 lifespan 的组合根装配：池就绪即挂跨域归属解析端口，
    # 审批发起的目标资源归属校验（R4-2 收口）在测试里走同一实现。
    from apps.api.resource_ownership import CompositeResourceOwnershipPort

    app.state.resource_ownership_port = CompositeResourceOwnershipPort(pools)
    try:
        yield pools
    finally:
        await pools.dispose()


@pytest.fixture
def project_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """项目规划域会话工厂（能读写 project_planning，不能写资产/资产被拒）。"""
    return db_pools.sessionmaker(DatabaseDomain.PROJECT_PLANNING)


@pytest.fixture
def asset_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """资产域会话工厂（能读写 asset_service，写不了 project_planning）。"""
    return db_pools.sessionmaker(DatabaseDomain.ASSET)


@pytest.fixture
def shot_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """镜头规划域会话工厂（能读写 shot_planning，写不了 project_planning）。"""
    return db_pools.sessionmaker(DatabaseDomain.SHOT_PLANNING)


@pytest.fixture
def qc_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """质量治理域会话工厂（能写 gate_decisions，资产域角色写不了）。"""
    return db_pools.sessionmaker(DatabaseDomain.QC_GOVERNANCE)


@pytest.fixture
def workflow_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """审批引擎域会话工厂（读写 workflow schema，其它业务域角色无权）。"""
    return db_pools.sessionmaker(DatabaseDomain.WORKFLOW)


@pytest.fixture
def relay_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """Outbox 投递域会话工厂（只读事件 + 改投递状态列）。"""
    return db_pools.sessionmaker(DatabaseDomain.OUTBOX_RELAY)


@pytest.fixture
def identity_sm(db_pools: DomainPools) -> async_sessionmaker[AsyncSession]:
    """身份域会话工厂（只读写 auth 吊销表）。"""
    return db_pools.sessionmaker(DatabaseDomain.IDENTITY)


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
    headers = make_test_headers()
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c
