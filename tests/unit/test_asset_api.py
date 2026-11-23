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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.shared.storage_locator import build_locator
from tests.conftest import TEST_TENANT_ID, make_test_token, set_test_scope

_SHA = "d" * 64
_LOCATOR = build_locator(_SHA, "png")
_LOCATOR_MP4 = build_locator(_SHA, "mp4")
# 资产归属键（SCOPE-3）：资产必须挂在真实项目上，归属判定才成立。
_PROJECT_ID = "project-1"


@pytest.fixture(scope="module", autouse=True)
async def _seed_project(db_sessionmaker) -> None:
    """播种归属项目（幂等）：资产无法脱离项目存在，测试也不能例外。"""
    async with db_sessionmaker() as session:
        await set_test_scope(session)
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, now(), 'tester', now(), '资产测试项目', 'B', NULL)
                ON CONFLICT (entity_id) DO NOTHING
                """
            ),
            {"eid": _PROJECT_ID, "tid": TEST_TENANT_ID},
        )
        await session.commit()


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
    # 契约 ASSET.VERSION.APPROVE 允许「导演（按职责）」；括号内为资质限定，
    # 解析后角色名就是「导演」，这里正是验证该主体名可机读（ADR-R08 G3 已关闭）。
    async with _client("director_1", ["导演"]) as c:
        yield c


@pytest.fixture
async def reader_client() -> AsyncClient:
    async with _client("producer_1", ["制片"]) as c:
        yield c


async def test_asset_lifecycle_end_to_end(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    asset_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"

    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
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
    # 用 asset_service_role 连接（asset_service 域数据只能由该域角色读，
    # project_planning_role 无 asset_service schema 的 USAGE——DATA-1 隔离）。
    async with asset_sessionmaker() as session:
        from sqlalchemy import text

        await set_test_scope(session)
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
    """导演不在 ASSET.VERSION.CREATE 的允许主体里 → 403（业务前拦截）。"""
    resp = await approver_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
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
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
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
        "project_id": _PROJECT_ID,
        "asset_type": "IMAGE",
        "sha256": _SHA,
        "storage_ref": _LOCATOR,
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
        "project_id": _PROJECT_ID,
        "asset_type": "IMAGE",
        "storage_ref": _LOCATOR,
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


async def test_read_asset_version_returns_envelope(
    worker_client: AsyncClient, reader_client: AsyncClient
) -> None:
    """ASSET.VERSION.READ：制片可读回同一信封（状态/版本号/证据引用）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201

    read_back = await reader_client.get(f"/assets/{asset_id}")
    assert read_back.status_code == 200, read_back.text
    body = read_back.json()
    assert body["asset_version_id"] == asset_id
    assert body["status"] == "GENERATED"
    assert body["entity_version"] == 1
    assert body["rights_manifest_ref"] == "rights-manifest-1"


async def test_read_asset_version_wrong_role_is_403(worker_client: AsyncClient) -> None:
    """Worker 不在 READ 允许主体里（写角色≠读角色），必须 403。"""
    resp = await worker_client.get("/assets/any-asset")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_read_missing_asset_is_404(reader_client: AsyncClient) -> None:
    """不存在与不可见统一 404，不泄漏跨租户存在性。"""
    resp = await reader_client.get(f"/assets/asset_{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# ASSET.STATUS.CHANGE（F06：APPROVED→GOLD 授权闭环）
# --------------------------------------------------------------------------- #
async def test_asset_status_change_to_gold_with_evidence(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    asset_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """完整主链：GENERATED→…→APPROVED→GOLD，进入 GOLD 需证据齐备 + 职责分离。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201

    reported = await qc_client.post(
        f"/assets/{asset_id}/qc-reports",
        json={
            "quality_profile_version": "qp-1",
            "detector_versions": {"blur": "1.0"},
            "findings": [{"code": "BLUR", "score": 0.12}],
        },
    )
    assert reported.status_code == 201

    approved = await approver_client.post(f"/assets/{asset_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    # 推进到 GOLD：approver（导演）≠ 创建人（worker_asset），职责分离成立；
    # 权利证据 + QC 证据齐备，转移表 APPROVED→GOLD 有边。
    to_gold = await approver_client.post(
        f"/assets/{asset_id}/status",
        json={"target_status": "GOLD"},
    )
    assert to_gold.status_code == 200, to_gold.text
    assert to_gold.json()["status"] == "GOLD"

    # 持久化核对：GOLD 是终态，转移表无出边，不可原地复活。
    async with asset_sessionmaker() as session:
        from sqlalchemy import text

        await set_test_scope(session)
        row = (
            await session.execute(
                text(
                    "SELECT status FROM asset_service.asset_versions WHERE entity_id = :e"
                ),
                {"e": asset_id},
            )
        ).one()
    assert row.status == "GOLD"


async def test_asset_status_change_illegal_transition_rejected(
    worker_client: AsyncClient, approver_client: AsyncClient
) -> None:
    """GENERATED→GOLD 直接跳：转移表无此边，运行时拒绝（不跳关）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201

    resp = await approver_client.post(
        f"/assets/{asset_id}/status",
        json={"target_status": "GOLD"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "STATE_TRANSITION_DENIED"


async def test_asset_status_change_wrong_role_is_403(
    worker_client: AsyncClient,
) -> None:
    """Worker 不在 ASSET.STATUS.CHANGE 允许主体里 → 403（业务前拦截）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201

    resp = await worker_client.post(
        f"/assets/{asset_id}/status",
        json={"target_status": "GOLD"},
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_asset_status_change_self_promotion_rejected(
    worker_client: AsyncClient,
) -> None:
    """创建人不能自行推进自身资产状态（职责分离，禁止自批）。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201

    # 用「导演」角色但 subject_id 仍是 worker_asset（同一主体）→ 职责分离拒绝。
    # 这里构造一个 subject 与 created_by 相同但角色为导演的 token。
    from tests.conftest import make_test_token as _mkt

    token = _mkt(subject_id="worker_asset", roles=["导演"])
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as self_promoter:
        resp = await self_promoter.post(
            f"/assets/{asset_id}/status",
            json={"target_status": "GOLD"},
        )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "STATE_TRANSITION_DENIED"


# ---------------------------------------------------------------------------
# storage_ref 入口收敛（P1-07 / #179）
# ---------------------------------------------------------------------------


async def test_free_form_storage_ref_rejected_at_entry(worker_client: AsyncClient) -> None:
    """旧的自由引用形态在协议层就被拒（422），不留到落库或读取时。

    整改前 ``storage_ref`` 是自由 dict，``{"uri": "s3://bucket/x.png"}`` 是
    「合法」的；现在入口就按受控定位符判定，错误现场紧贴调用方。
    """
    resp = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": "s3://bucket/frame.png",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_locator_with_mismatched_prefix_rejected_at_entry(
    worker_client: AsyncClient,
) -> None:
    """前两段与哈希不符 → 入口拒绝。

    这类定位符语法完全合法（正则能匹配），只有「前两段必须由哈希派生」这条
    才判得掉。若入口只校验形态，它会一路走到登记成功，然后在读取时表现为
    「内容不存在」——把拼写错误伪装成数据丢失。
    """
    resp = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": f"local://ff/ee/{_SHA}.png",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 422
    assert "由哈希派生" in resp.text


async def test_unknown_request_field_is_rejected(worker_client: AsyncClient) -> None:
    """未知字段必须 422 而不是静默忽略。

    静默忽略比 422 更危险：调用方以为字段生效了，实际被丢弃。这个模型上尤其
    要防——整改前 ``storage_ref`` 是自由 dict，多塞一个键是「合法」的。
    """
    resp = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT_ID,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "storage_hint": "worker 以为这个字段能指定落盘位置",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 422
