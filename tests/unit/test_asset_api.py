"""资产域 API 端到端集成测试（真实 PostgreSQL + 真实 JWT 鉴权）。

覆盖「执行闭环」的每一环：
- 契约动作 → 真实端点（POST /assets、qc-reports、approve）；
- 权限：角色不匹配在进入业务前就被 403 拦下（RBAC 由契约驱动）；
- 持久化：状态落库（RLS 会话内），状态机推进可见；
- 事件：审计事件与状态变更同事务（由 outbox 表承接，见 test_outbox）。

角色取值说明：契约 allowed_principals 里「导演按职责」这类带限定语的角色
无法与真实 JWT 角色精确对齐（已登记 ADR-R08）。因此批准路径使用契约里
明确的 ``QC`` 角色，而不是猜一个「导演」映射。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from tests.conftest import make_test_token

_SHA = "d" * 64


def _client(subject: str, roles: list[str]) -> AsyncClient:
    token = make_test_token(subject_id=subject, roles=roles)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def worker_client() -> AsyncClient:
    async with _client("worker_asset", ["Worker"]) as c:
        yield c


@pytest.fixture
async def qc_client() -> AsyncClient:
    async with _client("qc_asset", ["QC Worker"]) as c:
        yield c


@pytest.fixture
async def approver_client() -> AsyncClient:
    # 契约 ASSET.VERSION.APPROVE 允许 QC 角色；用不同主体保证职责分离。
    async with _client("qc_approver", ["QC"]) as c:
        yield c


async def test_asset_lifecycle_end_to_end(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"

    created = await worker_client.post(
        "/assets",
        json={
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": "s3://bucket/frame.png"},
            "source_asset_version_ids": [],
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "GENERATED"
    assert body["asset_version_id"] == asset_id

    reported = await qc_client.post(
        f"/assets/{asset_id}/qc-reports",
        json={
            "quality_profile_version": "qp-1",
            "detector_versions": {"blur": "1.0"},
            "findings": [{"code": "BLUR", "score": 0.12}],
        },
    )
    assert reported.status_code == 201, reported.text
    assert reported.json()["asset_version"]["status"] == "TECHNICAL_QC"

    approved = await approver_client.post(f"/assets/{asset_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    # 持久化核对：换一个会话直接读回，确认不是响应里的幻觉。
    async with db_sessionmaker() as session:
        from sqlalchemy import text

        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": "00000000-0000-0000-0000-000000000000"},
        )
        row = (
            await session.execute(
                text(
                    "SELECT status, entity_version FROM asset_service.asset_versions "
                    "WHERE entity_id = :e"
                ),
                {"e": asset_id},
            )
        ).one()
    assert row.status == "APPROVED"
    # 1(create) + 1(qc→TECHNICAL_QC) + 1(→HUMAN_REVIEW) + 1(→APPROVED) = 4
    assert row.entity_version == 4


async def test_create_asset_wrong_role_is_403(approver_client: AsyncClient) -> None:
    """QC 角色不在 ASSET.VERSION.CREATE 的允许主体里 → 403（业务前拦截）。"""
    resp = await approver_client.post(
        "/assets",
        json={
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": "s3://bucket/frame.png"},
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_worker_cannot_approve(worker_client: AsyncClient) -> None:
    """Worker 不得批准（批准是 HIGH 风险动作，允许主体不含 Worker）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": "s3://bucket/frame.png"},
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201
    resp = await worker_client.post(f"/assets/{asset_id}/approve")
    assert resp.status_code == 403


async def test_duplicate_asset_id_is_idempotent(worker_client: AsyncClient) -> None:
    """同 id 同内容重复登记 → 幂等返回同一版本（不是新建版本）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    payload = {
        "asset_type": "IMAGE",
        "sha256": _SHA,
        "storage_ref": {"uri": "s3://bucket/frame.png"},
        "entity_id": asset_id,
    }
    first = await worker_client.post("/assets", json=payload)
    second = await worker_client.post("/assets", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["entity_id"] == second.json()["entity_id"]
    assert first.json()["entity_version"] == second.json()["entity_version"]


async def test_overwriting_asset_version_is_rejected(worker_client: AsyncClient) -> None:
    """同 id 不同 sha256 = 覆盖旧版本，必须 409（不可变红线）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    base = {
        "asset_type": "IMAGE",
        "storage_ref": {"uri": "s3://bucket/frame.png"},
        "entity_id": asset_id,
    }
    first = await worker_client.post("/assets", json={**base, "sha256": _SHA})
    assert first.status_code == 201
    second = await worker_client.post("/assets", json={**base, "sha256": "e" * 64})
    assert second.status_code == 409
    assert second.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


async def test_empty_findings_rejected_by_validation(qc_client: AsyncClient) -> None:
    """findings 为空在协议层就被拒（缺证据不得产生报告）。"""
    resp = await qc_client.post(
        "/assets/not-exist/qc-reports",
        json={
            "quality_profile_version": "qp-1",
            "detector_versions": {"blur": "1.0"},
            "findings": [],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
