"""资源级授权链的 API 负向测试（AUTH-1 / SCOPE-3）。

为什么单开一个文件：第二轮审查 P1-03 的原文是「同一租户内不同组织的用户，
可能推进不属于自己的 Episode」——这类越权必须在真实 HTTP 端点 + 真实 JWT +
真实 PostgreSQL 上被证明拦得住，而不是用单元测试证明某个函数会抛异常。

覆盖（此前全部缺失，读路径更是从未被测）：
1. EPISODE.READ 跨组织 → 403（读路径，报告未提，本轮新发现）；
2. EPISODE.CREATE 往他人组织项目里造集数 → 403（写入即越权）；
3. EPISODE.STATUS.CHANGE 跨组织 → 403（报告 P1-03 原案）；
4. ASSET.VERSION.READ 跨组织 → 403（报告 P1-04 的后果）；
5. ASSET.VERSION.CREATE 往他人组织项目里登记资产 → 403；
6. 同组织内读写 → 正常（不误伤，防止「一律拒绝」的假通过）。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import TEST_TENANT_ID, make_test_token, set_test_scope

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()
_SHA = "a" * 64


async def _seed_organization(db_sessionmaker, org_id: uuid.UUID) -> None:
    """播种组织行（复合外键要求组织真实存在且与项目同租户）。

    必须以该组织为当前组织写入：organization_id 的组织谓词是
    ``organization_id = current_org``，它自己的行就是归属链的根（ADR-R24 §D3a-3）。
    """
    async with db_sessionmaker() as session:
        await set_test_scope(session, org_id)
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, :name, now(), 'tester', now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"org": org_id, "tid": TEST_TENANT_ID, "name": f"组织-{org_id.hex[:6]}"},
        )
        await session.commit()


def _client(
    *, organization_id: uuid.UUID | None, subject_id: str, roles: list[str] | None = None
) -> AsyncClient:
    """构造带指定 org claim / 角色的 client（真实 JWT + 真实 ASGI 端点）。"""
    from httpx import ASGITransport

    from apps.api.main import app

    token = make_test_token(
        subject_id=subject_id, roles=roles, organization_id=organization_id
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create_project(client: AsyncClient, org: uuid.UUID) -> str:
    resp = await client.post(
        "/projects",
        json={
            "project_name": f"范围测试_{uuid.uuid4().hex[:8]}",
            "project_tier": "B",
            "organization_id": str(org),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


@pytest.fixture(scope="module", autouse=True)
async def _organizations(db_sessionmaker) -> None:
    """本文件所有用例都依赖两个真实组织行。"""
    await _seed_organization(db_sessionmaker, _ORG_A)
    await _seed_organization(db_sessionmaker, _ORG_B)


async def test_project_create_into_other_organization_rejected() -> None:
    """以 A 组织成员身份创建「B 组织」项目 → 404（组织行不可见即 404）。

    此前该路径**没有判定**：路由拿 principal 的租户 + 请求体组织拼一条必然
    自洽的归属路径，组织是否真实属于本租户无人过问。此处钉住语义。
    """
    async with _client(organization_id=_ORG_A, subject_id="proj_owner") as owner:
        resp = await owner.post(
            "/projects",
            json={
                "project_name": "跨组织创建",
                "project_tier": "B",
                "organization_id": str(_ORG_B),
            },
        )
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_project_create_referencing_invisible_organization_is_not_found() -> None:
    """跨租户引用组织 → 404，且**不得**退化成 409。

    回归防护：组织解析缺失时，跨租户创建撞复合外键，被 Service 统一映射成
    ``CONCURRENT_MODIFICATION``（409，retryable=true）——把「无权/不可见」
    误报成「稍后重试即可」，既误导排障，也让调用方白白重试。
    归属不可见必须与不存在同形（404），这是 ``ownership`` 模块定死的语义。
    """
    from httpx import ASGITransport

    from apps.api.main import app

    foreign_tenant = uuid.uuid4()
    token = make_test_token(
        subject_id="proj_foreign",
        tenant_id=foreign_tenant,
        organization_id=_ORG_A,  # org claim 指向另一租户的组织
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        resp = await client.post(
            "/projects",
            json={
                "project_name": "跨租户创建",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_episode_create_into_other_organization_project_rejected() -> None:
    """往他组织项目里登记集数 → 404（写入即越权；组织边界下沉到库后不可见即 404）。"""
    async with _client(organization_id=_ORG_A, subject_id="ep_owner") as owner:
        project_id = await _create_project(owner, _ORG_A)

    async with _client(organization_id=_ORG_B, subject_id="ep_other") as other:
        resp = await other.post(
            "/episodes",
            json={
                "project_id": project_id,
                "episode_ref": "EP01",
                "title": "越权集数",
            },
        )
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_episode_read_across_organizations_rejected() -> None:
    """跨组织读集数 → 404（读路径；组织边界下沉到库后不可见即 404）。"""
    async with _client(organization_id=_ORG_A, subject_id="ep_owner") as owner:
        project_id = await _create_project(owner, _ORG_A)
        created = await owner.post(
            "/episodes",
            json={"project_id": project_id, "episode_ref": "EP01", "title": "本组织集数"},
        )
        assert created.status_code == 201, created.text
        episode_id = created.json()["entity_id"]

    async with _client(organization_id=_ORG_B, subject_id="ep_other") as other:
        resp = await other.get(f"/episodes/{episode_id}")
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_episode_status_change_across_organizations_rejected() -> None:
    """跨组织推进集数状态 → 404（第二轮审查 P1-03；不可见即 404）。"""
    async with _client(organization_id=_ORG_A, subject_id="ep_owner") as owner:
        project_id = await _create_project(owner, _ORG_A)
        created = await owner.post(
            "/episodes",
            json={"project_id": project_id, "episode_ref": "EP02", "title": "待推进集数"},
        )
        episode_id = created.json()["entity_id"]

    async with _client(organization_id=_ORG_B, subject_id="ep_other") as other:
        resp = await other.post(
            f"/episodes/{episode_id}/status", json={"target_status": "ACTIVE"}
        )
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_episode_read_in_same_organization_succeeds() -> None:
    """同组织成员读集数 → 200（不误伤）。"""
    async with _client(organization_id=_ORG_A, subject_id="ep_owner") as owner:
        project_id = await _create_project(owner, _ORG_A)
        created = await owner.post(
            "/episodes",
            json={"project_id": project_id, "episode_ref": "EP03", "title": "同组织集数"},
        )
        episode_id = created.json()["entity_id"]

    async with _client(organization_id=_ORG_A, subject_id="ep_colleague") as mate:
        resp = await mate.get(f"/episodes/{episode_id}")
    assert resp.status_code == 200


async def _create_asset(client: AsyncClient, project_id: str) -> str:
    resp = await client.post(
        "/assets",
        json={
            "project_id": project_id,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": f"s3://bucket/{uuid.uuid4().hex}.png"},
            "rights_manifest_ref": "rights-1",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_version_id"]


async def test_asset_create_into_other_organization_project_rejected() -> None:
    """往他组织项目登记资产 → 404（P1-04 写侧；不可见即 404）。"""
    async with _client(organization_id=_ORG_A, subject_id="proj_owner") as proj_owner:
        project_id = await _create_project(proj_owner, _ORG_A)

    async with _client(organization_id=_ORG_B, subject_id="asset_other", roles=["Worker"]) as other:
        resp = await other.post(
            "/assets",
            json={
                "project_id": project_id,
                "asset_type": "IMAGE",
                "sha256": _SHA,
                "storage_ref": {"uri": "s3://bucket/other.png"},
            },
        )
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_asset_read_across_organizations_rejected() -> None:
    """跨组织读资产 → 404（P1-04；不可见即 404）。"""
    async with _client(organization_id=_ORG_A, subject_id="proj_owner") as proj_owner:
        project_id = await _create_project(proj_owner, _ORG_A)
    async with _client(organization_id=_ORG_A, subject_id="asset_owner", roles=["Worker"]) as owner:
        asset_id = await _create_asset(owner, project_id)

    async with _client(organization_id=_ORG_B, subject_id="asset_reader") as other:
        resp = await other.get(f"/assets/{asset_id}")
    # ADR-R24 §D3a-7：组织边界下沉到数据库后，他组织的行**查不出来**，
    # 于是「存在但无权」与「不存在」在响应上同形（404）。403 只在调用方能
    # 看到该行时才可能出现——而看到本身就意味着边界没生效。
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_asset_read_in_same_organization_succeeds() -> None:
    """同组织成员读资产 → 200（不误伤）。"""
    async with _client(organization_id=_ORG_A, subject_id="proj_owner") as proj_owner:
        project_id = await _create_project(proj_owner, _ORG_A)
    async with _client(organization_id=_ORG_A, subject_id="asset_owner", roles=["Worker"]) as owner:
        asset_id = await _create_asset(owner, project_id)

    async with _client(organization_id=_ORG_A, subject_id="asset_reader") as mate:
        resp = await mate.get(f"/assets/{asset_id}")
    assert resp.status_code == 200
    assert resp.json()["asset_version_id"] == asset_id


async def test_asset_read_missing_project_returns_not_found() -> None:
    """归属解析不到项目 → 404（不泄漏存在性，也不静默放行）。"""
    async with _client(organization_id=_ORG_A, subject_id="asset_owner", roles=["Worker"]) as owner:
        resp = await owner.post(
            "/assets",
            json={
                "project_id": f"missing_{uuid.uuid4().hex[:8]}",
                "asset_type": "IMAGE",
                "sha256": _SHA,
                "storage_ref": {"uri": "s3://bucket/none.png"},
            },
        )
    assert resp.status_code == 404
