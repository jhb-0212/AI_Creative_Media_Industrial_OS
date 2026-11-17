"""DELIVERY.MANIFEST.RELEASE 的 API 端到端测试（真实 JWT + RLS + 数据库）。

验证放行的结构性红线：
1. 只有 VALIDATED 可放行：DRAFT 清单的 RELEASE 被领域层拒绝（409）；
2. 职责分离：生成清单的人不能自己放行（applicant_subject_id 来自清单
   created_by，服务端事实，不是客户端声明）；
3. 角色与风险：非授权主体 403、Agent 不得过 CRITICAL；
4. 项目级隔离：令牌范围不含清单项目时 403；
5. 成功放行产出 RELEASED 事件、状态落库、响应字段集符合契约。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from apps.api.main import app
from packages.shared.database import build_migration_url
from tests.conftest import TEST_TENANT_ID, make_test_token

pytestmark = pytest.mark.asyncio

_PROJECT = "proj_release_api"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_manifest(
    admin_engine: AsyncEngine,
    *,
    status: str,
    created_by: str,
    project_id: str = _PROJECT,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
) -> str:
    entity_id = f"manifest-release-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.delivery_manifests (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    project_id, asset_refs, checksum, picture_lock_ref,
                    qc_final_ref, rebuild_recipe, rights_evidence_refs, spec_ref
                ) VALUES (
                    :eid, 'DeliveryManifest', 205, 3,
                    :status, 'asset_service', :tid, :tid,
                    now(), :creator, now(), 'approval-event-1',
                    :pid, '["asset-a"]'::jsonb, repeat('0', 64), 'picture-lock-1',
                    'qc-final-1', 'recipe://spec/v1', '["rights-1"]'::jsonb, 'spec-1'
                )
                """
            ),
            {
                "eid": entity_id,
                "status": status,
                "tid": tenant_id,
                "creator": created_by,
                "pid": project_id,
            },
        )
    return entity_id


def _client(
    subject: str,
    roles: list[str],
    *,
    project_ids: list[str] | None = None,
    subject_type: str = "human",
) -> AsyncClient:
    token = make_test_token(
        subject_id=subject,
        roles=roles,
        project_ids=project_ids if project_ids is not None else ["*"],
        subject_type=subject_type,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_validated_manifest_can_be_released(
    admin_engine: AsyncEngine,
) -> None:
    manifest_id = await _seed_manifest(
        admin_engine, status="VALIDATED", created_by="creator-a"
    )
    async with _client("release-owner", ["交付负责人"]) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "RELEASED"
    assert body["manifest_id"] == manifest_id
    # 响应字段集恰为契约字段（不泄漏 project_id/tenant_id）。
    assert "project_id" not in body and "tenant_id" not in body

    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT status, entity_version FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": manifest_id},
            )
        ).one()
    assert row.status == "RELEASED"
    assert row.entity_version == 4


async def test_draft_manifest_cannot_be_released(admin_engine: AsyncEngine) -> None:
    manifest_id = await _seed_manifest(
        admin_engine, status="DRAFT", created_by="creator-a"
    )
    async with _client("release-owner", ["交付负责人"]) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "STATE_TRANSITION_DENIED"


async def test_creator_cannot_release_own_manifest(
    admin_engine: AsyncEngine,
) -> None:
    """职责分离：生成清单的人不能自己放行（W2 真正生效，非豁免）。"""
    manifest_id = await _seed_manifest(
        admin_engine, status="VALIDATED", created_by="self-creator"
    )
    async with _client("self-creator", ["交付负责人"]) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_unauthorized_role_is_403(admin_engine: AsyncEngine) -> None:
    manifest_id = await _seed_manifest(
        admin_engine, status="VALIDATED", created_by="creator-a"
    )
    async with _client("worker-release", ["Worker"]) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 403, response.text


async def test_agent_cannot_release_critical(admin_engine: AsyncEngine) -> None:
    """Agent 不得执行 CRITICAL 动作（红线）。"""
    manifest_id = await _seed_manifest(
        admin_engine, status="VALIDATED", created_by="creator-a"
    )
    async with _client(
        "agent-release", ["交付负责人"], subject_type="agent"
    ) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 403, response.text


async def test_cross_project_release_is_not_found(admin_engine: AsyncEngine) -> None:
    """令牌项目范围不含清单项目 → 清单不可见（RLS），表现为 404。

    刻意不返回 403：403 会确认"这张清单存在，只是你没权限"，泄漏跨项目资源的
    存在性。与 D1 的 get_manifest「不可见即 NOT_FOUND」同口径。
    """
    manifest_id = await _seed_manifest(
        admin_engine,
        status="VALIDATED",
        created_by="creator-a",
        project_id="proj_release_other",
    )
    async with _client(
        "release-owner", ["交付负责人"], project_ids=[_PROJECT]
    ) as c:
        response = await c.post(f"/delivery-manifests/{manifest_id}/release")
    assert response.status_code == 404, response.text
