"""项目级与组织级隔离的负向测试（B3 归属链，ADR-R17）。

要证明的命题只有两句：

1. **跨项目**：持有项目 A 授权的令牌，看不到、也写不进项目 B 的资源。
2. **跨组织**：即使持有"本组织全部项目"范围，也看不到另一个组织的项目。

证明方式刻意分两层，两层都要有：
- **数据库层**（本文件多数用例通过真实 HTTP → 真实域角色 → 真实 RLS）：
  越权被 PostgreSQL 拒绝。应用层漏写检查也不会破防。
- **应用层**：写入入口对请求体里显式携带的 project_id 给出 403，
  而不是把数据库的 42501 暴露成 500。

为什么这些用例值得单独一个文件：
- 它们的失败方式很隐蔽——不是报错，而是"多看到一行"。散落在各处时，
  一旦有人为了让某条业务用例变绿而放宽策略，不会有任何提示。
- 每个用例都从"数据确实存在"开始（先用全范围令牌创建），再切换受限令牌
  去访问。若省略这一步，用例会因为"本来就没有数据"而以 404 通过，
  变成假绿。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from tests.conftest import TEST_TENANT_ID, make_test_token

_SHA_A = "e" * 64
_SHA_B = "f" * 64
_OTHER_ORG = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _client(
    subject: str,
    roles: list[str],
    *,
    project_ids: list[str] | None = None,
    organization_id: str | None = None,
) -> AsyncClient:
    """构造带指定范围的客户端（范围来自令牌，不是请求体）。"""
    token = make_test_token(
        subject_id=subject,
        roles=roles,
        project_ids=project_ids,
        organization_id=organization_id,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def admin() -> AsyncClient:
    """全组织范围 + 项目创建权限（制片）：用它准备"别人的数据"。"""
    async with _client("producer_scope", ["制片"]) as c:
        yield c


async def test_project_creation_requires_org_wide_scope(
    admin: AsyncClient,
) -> None:
    """新建项目要求"本组织全部项目"范围：项目级用户不得自锁式建项目。

    新建项目的 id 不属于任何既有范围。若放行，调用方写进去之后自己也读不回来
    （自锁）；若为此放宽数据库 WITH CHECK，就等于允许往看不见的项目里写数据。
    因此这里要求 ``*``。
    """
    async with _client(
        "scoped_director",
        ["制片"],
        project_ids=["proj_existing_only"],
    ) as scoped:
        resp = await scoped.post(
            "/projects",
            json={
                "project_name": "范围受限者新建的项目",
                "project_tier": "B",
                "entity_id": f"proj_scoped_{uuid.uuid4().hex[:8]}",
            },
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_project_of_other_org_is_invisible(admin: AsyncClient) -> None:
    """组织 A 的项目，对组织 B 的令牌不可见（返回 404，不确认存在性）。"""
    project_id = f"proj_org_a_{uuid.uuid4().hex[:8]}"
    created = await admin.post(
        "/projects",
        json={
            "project_name": "组织 A 的项目",
            "project_tier": "B",
            "entity_id": project_id,
        },
    )
    assert created.status_code == 201, created.text
    # 归属由服务端从 Principal 写入：请求体无法指定组织。
    assert created.json()["organization_id"] == str(TEST_TENANT_ID)

    async with _client("org_b_director", ["导演"], organization_id=str(_OTHER_ORG)) as org_b:
        resp = await org_b.get(f"/projects/{project_id}")
    assert resp.status_code == 404, resp.text


async def test_scoped_token_cannot_read_other_project(admin: AsyncClient) -> None:
    """持有项目 A 范围的令牌读项目 B → 404（同组织内跨项目同样被拒）。"""
    project_a = f"proj_scope_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_scope_b_{uuid.uuid4().hex[:8]}"
    for pid in (project_a, project_b):
        created = await admin.post(
            "/projects",
            json={"project_name": f"项目 {pid}", "project_tier": "B", "entity_id": pid},
        )
        assert created.status_code == 201, created.text

    async with _client("scoped_a", ["导演"], project_ids=[project_a]) as scoped_a:
        own = await scoped_a.get(f"/projects/{project_a}")
        assert own.status_code == 200, own.text
        # 自证数据确实存在：受限令牌看不到同行邻居，不是因为库里没有。
        other = await scoped_a.get(f"/projects/{project_b}")
    assert other.status_code == 404, other.text


async def test_scoped_token_cannot_hide_behind_episode_ownership(
    admin: AsyncClient,
) -> None:
    """跨项目建集数 → 归属校验看到的是"项目不存在"（RLS 已把项目过滤掉）。"""
    project_a = f"proj_ep_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_ep_b_{uuid.uuid4().hex[:8]}"
    for pid in (project_a, project_b):
        created = await admin.post(
            "/projects",
            json={"project_name": f"项目 {pid}", "project_tier": "B", "entity_id": pid},
        )
        assert created.status_code == 201, created.text

    async with _client("scoped_b1", ["导演"], project_ids=[project_b]) as scoped:
        # 自己的项目：可以登记集数。
        ok = await scoped.post(
            "/episodes",
            json={
                "project_id": project_b,
                "episode_ref": "EP001",
                "title": "属于自己项目的集",
                "entity_id": f"ep_own_{uuid.uuid4().hex[:8]}",
            },
        )
        assert ok.status_code == 201, ok.text
        # 别人的项目：被当成不存在（不确认存在性，也不泄漏项目名）。
        denied = await scoped.post(
            "/episodes",
            json={
                "project_id": project_a,
                "episode_ref": "EP001",
                "title": "越界登记",
                "entity_id": f"ep_bad_{uuid.uuid4().hex[:8]}",
            },
        )
    assert denied.status_code == 404, denied.text
    assert denied.json()["error_code"] == "NOT_FOUND"


async def test_asset_create_in_other_project_is_403(admin: AsyncClient) -> None:
    """资产写入入口对显式 project_id 直接 403（不依赖数据库错误码翻译）。

    为什么这条是 403 而不是 404：project_id 是调用方自己填的，拒绝时不需要
    掩饰存在性；而资产域没有项目表的读权（B1 按域授权），也无从判断存在性。
    数据库的项目范围策略是同一事实的第二道防线。
    """
    project_a = f"proj_asset_a_{uuid.uuid4().hex[:8]}"
    created = await admin.post(
        "/projects",
        json={"project_name": "项目 A", "project_tier": "B", "entity_id": project_a},
    )
    assert created.status_code == 201, created.text

    async with _client("worker_scoped", ["Worker"], project_ids=["proj_unrelated"]) as worker:
        denied = await worker.post(
            "/assets",
            json={
                "project_id": project_a,
                "asset_type": "IMAGE",
                "sha256": _SHA_A,
                "storage_ref": {"uri": "s3://bucket/a.png"},
            },
        )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_asset_of_other_project_is_invisible(admin: AsyncClient) -> None:
    """同组织内：项目 A 的令牌读项目 B 的资产 → 404（数据库层过滤）。"""
    project_a = f"proj_av_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_av_b_{uuid.uuid4().hex[:8]}"
    for pid in (project_a, project_b):
        created = await admin.post(
            "/projects",
            json={"project_name": f"项目 {pid}", "project_tier": "B", "entity_id": pid},
        )
        assert created.status_code == 201, created.text

    asset_a = f"asset_scope_a_{uuid.uuid4().hex[:8]}"
    async with _client("worker_full", ["Worker"]) as worker:
        created_asset = await worker.post(
            "/assets",
            json={
                "project_id": project_a,
                "asset_type": "IMAGE",
                "sha256": _SHA_A,
                "storage_ref": {"uri": "s3://bucket/a.png"},
                "entity_id": asset_a,
            },
        )
        assert created_asset.status_code == 201, created_asset.text

    async with _client("reader_b", ["制片"], project_ids=[project_b]) as reader_b:
        denied = await reader_b.get(f"/assets/{asset_a}")
    assert denied.status_code == 404, denied.text


async def test_qc_report_of_other_project_is_invisible(admin: AsyncClient) -> None:
    """QC 证据同样受项目范围约束：跨项目读不到它所属的资产。

    证据与资产共用同一组归属键（tenant/organization/project），
    因此"证据可以跨项目漂流"这条缺口被同一个策略关掉。
    """
    project_a = f"proj_qc_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_qc_b_{uuid.uuid4().hex[:8]}"
    for pid in (project_a, project_b):
        created = await admin.post(
            "/projects",
            json={"project_name": f"项目 {pid}", "project_tier": "B", "entity_id": pid},
        )
        assert created.status_code == 201, created.text

    asset_id = f"asset_qc_{uuid.uuid4().hex[:8]}"
    async with _client("worker_qc", ["Worker"]) as worker:
        assert (
            await worker.post(
                "/assets",
                json={
                    "project_id": project_a,
                    "asset_type": "IMAGE",
                    "sha256": _SHA_B,
                    "storage_ref": {"uri": "s3://bucket/qc.png"},
                    "entity_id": asset_id,
                },
            )
        ).status_code == 201

    async with _client("qc_scoped", ["QC Worker"], project_ids=[project_b]) as qc:
        denied = await qc.post(
            f"/assets/{asset_id}/qc-reports",
            json={
                "quality_profile_version": "qp-1",
                "detector_versions": {"blur": "1.0"},
                "findings": [{"code": "BLUR"}],
            },
        )
    assert denied.status_code == 404, denied.text


async def test_empty_scope_sees_nothing(admin: AsyncClient) -> None:
    """空项目范围（令牌没声明任何项目）→ 一个项目也看不见（fail-closed）。"""
    project_id = f"proj_empty_scope_{uuid.uuid4().hex[:8]}"
    created = await admin.post(
        "/projects",
        json={"project_name": "项目", "project_tier": "B", "entity_id": project_id},
    )
    assert created.status_code == 201, created.text

    # project_ids=[] 显式传空列表：令牌里就是空集，不是"缺省即全部"。
    async with _client("no_scope", ["制片"], project_ids=[]) as nobody:
        resp = await nobody.get(f"/projects/{project_id}")
    assert resp.status_code == 404, resp.text
