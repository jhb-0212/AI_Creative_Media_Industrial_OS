"""复合外键归属完整性的集成测试（决策 2 + R2/R9）。

核心验证：script_revisions 的复合外键 (project_id, episode_ref) →
episodes(project_id, episode_ref) 在**数据库层**锁死「跨项目乱挂载」——
即使绕过 Service 层归属验证，直接向 script_revisions 写入一个引用不存在
Episode 的 (project_id, episode_ref)，PostgreSQL 也会因外键违反而拒绝。

这证明归属不变量落到了最可靠的层级（数据库），而非仅靠应用层查询校验。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from apps.api.main import app
from packages.shared.database import get_sessionmaker
from tests.conftest import make_test_token


@pytest.fixture
async def client() -> AsyncClient:
    """带认证的 client（身份从 JWT 解析，走真实鉴权路径）。"""
    transport = ASGITransport(app=app)
    token = make_test_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c


async def test_composite_fk_rejects_dangling_episode(client: AsyncClient) -> None:
    """数据库层拒绝引用不存在 Episode 的 script_revision。

    场景：直接 INSERT 一条 (project_id, episode_ref) 无对应 Episode 的
    script_revisions 记录，绕过 Service 层归属验证，验证外键兜底。
    """
    # 用一个不存在的 project/episode 组合，确保无对应 Episode。
    dangling_project = f"proj_dangling_{uuid.uuid4().hex[:8]}"
    dangling_ref = f"EP{uuid.uuid4().hex[:4].upper()}"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # 注入 RLS 上下文（app_role 连接下 RLS 生效，需设置 tenant 才能写）。
        await session.execute(
            text(
                "SELECT set_config('app.current_tenant_id', :tid, true)"
            ),
            {"tid": "00000000-0000-0000-0000-000000000000"},
        )
        # 直接 SQL INSERT 一条悬空的 script_revision（绕过 ORM/Service）。
        insert = text(
            """
            INSERT INTO project_planning.script_revisions
                (entity_id, entity_type, schema_version, entity_version, status,
                 owner_module, tenant_id, created_at, created_by, updated_at, audit_ref,
                 project_id, episode_ref, title)
            VALUES
                (:entity_id, 'ScriptRevision', 205, 1, 'DRAFT',
                 'project-planning', :tenant_id, now(), :created_by, now(), NULL,
                 :project_id, :episode_ref, '悬空剧本')
            """
        )
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                insert,
                {
                    "entity_id": f"scr_dangling_{uuid.uuid4().hex[:8]}",
                    "tenant_id": "00000000-0000-0000-0000-000000000000",
                    "created_by": "user_dangling",
                    "project_id": dangling_project,
                    "episode_ref": dangling_ref,
                },
            )
            await session.commit()
        await session.rollback()

    # 断言违反的是复合外键约束（而非其它约束）。
    assert "fk_script_revisions_episode" in str(exc_info.value.orig)


async def test_composite_fk_allows_valid_episode(client: AsyncClient) -> None:
    """正例：引用真实存在的 Episode 时，外键不拒绝（证明约束不误伤）。"""
    project_id = f"proj_fk_ok_{uuid.uuid4().hex[:8]}"
    # 通过 API 正常建立 project + episode（有归属）。
    rp = await client.post(
        "/projects",
        json={
            "project_name": f"项目_{project_id}",
            "project_tier": "B",
            "entity_id": project_id,
        },
    )
    assert rp.status_code == 201
    re_ = await client.post(
        "/episodes",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "集数_EP001",
        },
    )
    assert re_.status_code == 201

    # 正常导入剧本（引用真实 Episode），应成功。
    rs = await client.post(
        "/scripts/import",
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "title": "合法剧本",
            "entity_id": f"scr_fk_ok_{uuid.uuid4().hex[:8]}",
        },
    )
    assert rs.status_code == 201
    assert rs.json()["status"] == "DRAFT"
