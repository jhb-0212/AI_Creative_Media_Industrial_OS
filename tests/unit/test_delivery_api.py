"""交付清单 API 端到端测试（真实 PostgreSQL + 真实 JWT 鉴权）。

覆盖 `DELIVERY.MANIFEST.CREATE` 的执行闭环，以及三条"结构性承诺"的实测：

1. **校验和由服务端算**：请求体不接受 `checksum`；客户端塞进来的值不生效
   （响应哈希 == 同一份内容推导出的哈希）。
2. **不声明未发生的 Gate PASS**：响应字段集里没有任何 `gate_pass` 类布尔字段，
   而只有证据引用（`qc_final_ref` / `picture_lock_ref` / `rights_evidence_refs`）。
3. **只生成 DRAFT**：即使后续有审批动作，CREATE 出来的状态恒为 DRAFT。

角色取值说明：契约 `allowed_principals` 为「交付服务/授权制片」（`/` 是 OR 语义），
因此令牌角色用「授权制片」；Worker 不在其中，用它可以验证业务前拦截。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from tests.conftest import apply_db_context, make_test_headers

_SHA = "f" * 64
_PROJECT = "proj_delivery_api"
_OTHER_PROJECT = "proj_delivery_other"


def _client(subject: str, roles: list[str]) -> AsyncClient:
    headers = make_test_headers(subject_id=subject, roles=roles)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


@pytest.fixture
async def worker_client() -> AsyncClient:
    async with _client("worker_delivery", ["Worker"]) as c:
        yield c


@pytest.fixture
async def qc_client() -> AsyncClient:
    async with _client("qc_delivery", ["QC Worker"]) as c:
        yield c


@pytest.fixture
async def approver_client() -> AsyncClient:
    async with _client("director_delivery", ["导演"]) as c:
        yield c


@pytest.fixture
async def delivery_client() -> AsyncClient:
    """契约允许主体「授权制片」。"""
    async with _client("producer_delivery", ["授权制片"]) as c:
        yield c


async def _approved_asset(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    *,
    project_id: str = _PROJECT,
) -> str:
    """走完整资产链（GENERATED → TECHNICAL_QC → APPROVED），返回资产 id。

    刻意不直接插库：清单校验的正是"被引用版本真的走完了审批"，
    用插库绕过去等于跳过了被测对象的前置事实。
    """
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": project_id,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": "s3://bucket/frame.png"},
            "source_asset_version_ids": [],
            "rights_manifest_ref": "rights-manifest-1",
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201, created.text
    reported = await qc_client.post(
        f"/assets/{asset_id}/qc-reports",
        json={
            "quality_profile_version": "qp-1",
            "detector_versions": {"blur": "1.0"},
            "findings": [{"code": "BLUR", "score": 0.1}],
        },
    )
    assert reported.status_code == 201, reported.text
    approved = await approver_client.post(f"/assets/{asset_id}/approve")
    assert approved.status_code == 200, approved.text
    return asset_id


def _body(asset_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": _PROJECT,
        "spec_ref": "spec-1",
        "qc_final_ref": "qc-final-1",
        "picture_lock_ref": "picture-lock-1",
        "asset_refs": [asset_id],
        "rights_evidence_refs": ["rights-evidence-1"],
        "rebuild_recipe": "recipe://spec-1/v1",
    }
    payload.update(overrides)
    return payload


async def test_create_manifest_end_to_end(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    asset_id = await _approved_asset(
        worker_client, qc_client, approver_client
    )

    resp = await delivery_client.post("/delivery-manifests", json=_body(asset_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "DRAFT"
    assert body["entity_type"] == "DeliveryManifest"
    assert body["entity_id"] == body["manifest_id"]
    assert body["asset_refs"] == [asset_id]
    assert len(body["checksum"]) == 64
    # 契约未声明 project_id / tenant_id：私有隔离列不得出现在响应里。
    assert "project_id" not in body
    assert "tenant_id" not in body

    # 持久化核对：换一个会话直接读回，确认不是响应里的幻觉。
    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(_PROJECT,))
        row = (
            await session.execute(
                text(
                    "SELECT status, checksum, project_id, asset_refs "
                    "FROM asset_service.delivery_manifests WHERE entity_id = :e"
                ),
                {"e": body["manifest_id"]},
            )
        ).one()
    assert row.status == "DRAFT"
    assert row.checksum == body["checksum"]
    assert row.project_id == _PROJECT
    assert list(row.asset_refs) == [asset_id]


async def test_checksum_is_computed_by_server(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """客户端塞的 checksum 不生效：同一份内容必须产出同一哈希。"""
    asset_id = await _approved_asset(worker_client, qc_client, approver_client)

    first = await delivery_client.post(
        "/delivery-manifests", json=_body(asset_id, checksum="0" * 64)
    )
    second = await delivery_client.post("/delivery-manifests", json=_body(asset_id))
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # 两次内容相同 → 哈希相同；若客户端能控制 checksum，第一次会是全 0。
    assert first.json()["checksum"] == second.json()["checksum"]
    assert first.json()["checksum"] != "0" * 64


async def test_manifest_response_declares_no_gate_pass_field(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """C6：清单不得声明未发生的 Gate PASS —— 由字段集缺项结构性保证。"""
    asset_id = await _approved_asset(worker_client, qc_client, approver_client)
    resp = await delivery_client.post("/delivery-manifests", json=_body(asset_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    gate_like = [
        key for key in body if "gate" in key.lower() and "pass" in key.lower()
    ]
    assert gate_like == []
    # 只有证据引用，没有结论字段。
    for ref_field in ("qc_final_ref", "picture_lock_ref", "rights_evidence_refs"):
        assert ref_field in body


async def test_missing_rights_evidence_is_422(delivery_client: AsyncClient) -> None:
    """缺权利证据在协议层就被拒（空列表过不了 min_length）。"""
    resp = await delivery_client.post(
        "/delivery-manifests", json=_body("asset-any", rights_evidence_refs=[])
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_empty_asset_refs_is_422(delivery_client: AsyncClient) -> None:
    """空引用列表同样在协议层拒绝（清单必须引用具体版本）。"""
    resp = await delivery_client.post(
        "/delivery-manifests", json=_body("asset-any", asset_refs=[])
    )
    assert resp.status_code == 422


async def test_unknown_asset_reference_is_422(delivery_client: AsyncClient) -> None:
    """引用不存在的版本 → 拒绝（交付时才发现缺件的成本更高）。"""
    resp = await delivery_client.post(
        "/delivery-manifests", json=_body(f"asset_{uuid.uuid4().hex[:12]}")
    )
    assert resp.status_code == 422, resp.text
    assert "不存在" in resp.json()["message"]


async def test_unapproved_asset_is_rejected(
    worker_client: AsyncClient, delivery_client: AsyncClient
) -> None:
    """只创建未审批的资产（GENERATED）→ 不得进入交付清单。"""
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    created = await worker_client.post(
        "/assets",
        json={
            "project_id": _PROJECT,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": {"uri": "s3://bucket/frame.png"},
            "entity_id": asset_id,
        },
    )
    assert created.status_code == 201, created.text

    resp = await delivery_client.post("/delivery-manifests", json=_body(asset_id))
    assert resp.status_code == 422, resp.text
    assert "APPROVED" in resp.json()["message"]


async def test_cross_project_asset_is_rejected(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """引用其他项目的资产 → 拒绝（同租户跨项目也要拦）。"""
    other_asset = await _approved_asset(
        worker_client, qc_client, approver_client, project_id=_OTHER_PROJECT
    )
    resp = await delivery_client.post(
        "/delivery-manifests", json=_body(other_asset)
    )
    assert resp.status_code == 422, resp.text
    assert "其他项目" in resp.json()["message"]


async def test_duplicate_manifest_is_idempotent(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """同 id 同内容重复提交 → 返回同一份清单（不产生第二个版本）。"""
    asset_id = await _approved_asset(worker_client, qc_client, approver_client)
    manifest_id = f"manifest_{uuid.uuid4().hex[:12]}"

    first = await delivery_client.post(
        "/delivery-manifests", json=_body(asset_id, manifest_id=manifest_id)
    )
    second = await delivery_client.post(
        "/delivery-manifests", json=_body(asset_id, manifest_id=manifest_id)
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["checksum"] == second.json()["checksum"]
    assert first.json()["entity_version"] == second.json()["entity_version"]


async def test_rewriting_manifest_is_409(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """同 id 不同内容 = 改写已发出的清单 → 409（每个版本不可变）。"""
    asset_id = await _approved_asset(worker_client, qc_client, approver_client)
    manifest_id = f"manifest_{uuid.uuid4().hex[:12]}"

    first = await delivery_client.post(
        "/delivery-manifests", json=_body(asset_id, manifest_id=manifest_id)
    )
    assert first.status_code == 201, first.text
    second = await delivery_client.post(
        "/delivery-manifests",
        json=_body(asset_id, manifest_id=manifest_id, spec_ref="spec-2"),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


async def test_wrong_role_is_403(worker_client: AsyncClient) -> None:
    """Worker 不在允许主体「交付服务/授权制片」里 → 业务前 403。"""
    resp = await worker_client.post("/delivery-manifests", json=_body("asset-any"))
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_unknown_field_is_ignored_not_trusted(
    worker_client: AsyncClient,
    qc_client: AsyncClient,
    approver_client: AsyncClient,
    delivery_client: AsyncClient,
) -> None:
    """请求体里塞 `status` 不生效：状态由动作语义决定，不由调用方决定。"""
    asset_id = await _approved_asset(worker_client, qc_client, approver_client)
    resp = await delivery_client.post(
        "/delivery-manifests", json=_body(asset_id, status="RELEASED")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "DRAFT"
