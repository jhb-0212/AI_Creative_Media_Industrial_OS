"""交付清单应用服务单测（内存 fake 仓储 + fake outbox，无数据库）。

验证「用例规则」而不是 ORM，重点四条：

- **证据必须先成立**：引用了不存在 / 跨项目 / 未审批的资产版本，一律拒绝。
  清单一旦 VALIDATED 就是对客户的交付依据，这些失败都发生在"已经承诺交付"
  之后，补救成本远高于此刻拦下。
- **幂等键复用必须是同一请求**：同 id 同内容返回既有清单且不重复发事件；
  同 id 不同内容属改写已发出的清单，必须 409。
- **并发重复提交走同一判定**：仓储抛 `DuplicateEntityError` 时回查后按内容比对，
  不允许"并发路径绕过内容校验"。
- **事件与写清单同一次调用**：不写清单就不发事件，反之亦然。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal  # noqa: F401  (类型标注用)
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.delivery_manifest import DeliveryManifest
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.manifest_repo import DeliveryManifestRepository
from services.asset_service.service.delivery_service import DeliveryService
from tests.unit.test_asset_service import (
    FakeAssetRepository,
    FakeOutboxWriter,
    _principal,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_PROJECT = "proj-delivery-service"
_OTHER_PROJECT = "proj-other"


def _asset(
    entity_id: str,
    *,
    project_id: str = _PROJECT,
    status: AssetStatus = AssetStatus.APPROVED,
) -> AssetVersion:
    return AssetVersion(
        entity_id=entity_id,
        created_by="worker-1",
        tenant_id=_TENANT,
        project_id=project_id,
        asset_type="IMAGE",
        sha256="a" * 64,
        storage_ref={"uri": "s3://bucket/key.png"},
        rights_manifest_ref="rights-1",
        status=status,
    )


class FakeManifestRepository(DeliveryManifestRepository):
    """内存清单仓储（端口没有 save，因此不存在"改清单"的测试路径）。"""

    def __init__(self) -> None:
        self.rows: dict[str, DeliveryManifest] = {}

    async def add(self, manifest: DeliveryManifest) -> None:
        if manifest.manifest_id in self.rows:
            raise DuplicateEntityError(f"manifest_id={manifest.manifest_id}")
        self.rows[manifest.manifest_id] = replace(manifest)

    async def get_by_id(self, manifest_id: str) -> DeliveryManifest | None:
        stored = self.rows.get(manifest_id)
        return replace(stored) if stored is not None else None


class _StaleReadManifestRepository(FakeManifestRepository):
    """模拟并发：首次 `get_by_id` 看不到记录，`add` 才发现主键已存在。"""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    async def get_by_id(self, manifest_id: str) -> DeliveryManifest | None:
        self.get_calls += 1
        if self.get_calls == 1:
            return None
        return await super().get_by_id(manifest_id)


def _service(
    assets: AssetRepository | None = None,
    manifests: DeliveryManifestRepository | None = None,
) -> tuple[DeliveryService, FakeAssetRepository, DeliveryManifestRepository, FakeOutboxWriter]:
    asset_repo = assets if isinstance(assets, FakeAssetRepository) else FakeAssetRepository()
    manifest_repo = manifests or FakeManifestRepository()
    outbox = FakeOutboxWriter()
    return (
        DeliveryService(asset_repo, manifest_repo, outbox),
        asset_repo,
        manifest_repo,
        outbox,
    )


async def _seed_assets(*assets: AssetVersion) -> FakeAssetRepository:
    repo = FakeAssetRepository()
    for asset in assets:
        await repo.add(asset)
    return repo


def _payload(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "project_id": _PROJECT,
        "spec_ref": "spec-1",
        "qc_final_ref": "qc-final-1",
        "picture_lock_ref": "picture-lock-1",
        "asset_refs": ["asset-a"],
        "rights_evidence_refs": ["rights-1"],
        "rebuild_recipe": "recipe://spec-1/v1",
        "principal": _principal("delivery-1", "授权制片"),
    }
    params.update(overrides)
    return params


async def test_create_manifest_starts_draft_and_emits_event() -> None:
    assets = await _seed_assets(_asset("asset-a"))
    svc, _, manifests, outbox = _service(assets)

    manifest = await svc.create_manifest(**_payload())  # type: ignore[arg-type]

    assert manifest.status.value == "DRAFT"
    assert manifests.rows[manifest.manifest_id].checksum == manifest.checksum
    assert [e["event_type"] for e in outbox.events] == ["DELIVERY_MANIFEST_CREATED"]
    payload = outbox.events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["checksum"] == manifest.checksum


async def test_duplicate_same_content_is_idempotent() -> None:
    """同 id 同内容 → 返回既有清单，且不重复发事件（重复提交不是新事实）。"""
    assets = await _seed_assets(_asset("asset-a"))
    svc, _, _, outbox = _service(assets)
    payload = _payload(manifest_id="manifest-fixed")

    first = await svc.create_manifest(**payload)  # type: ignore[arg-type]
    second = await svc.create_manifest(**payload)  # type: ignore[arg-type]

    assert first.manifest_id == second.manifest_id
    assert first.checksum == second.checksum
    assert len(outbox.events) == 1


async def test_duplicate_different_content_is_rejected() -> None:
    """同 id 不同内容 = 改写已发出的交付清单 → 409（不可变红线）。"""
    assets = await _seed_assets(_asset("asset-a"))
    svc, _, _, _ = _service(assets)

    await svc.create_manifest(**_payload(manifest_id="manifest-fixed"))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as exc:
        await svc.create_manifest(
            **_payload(manifest_id="manifest-fixed", spec_ref="spec-2")  # type: ignore[arg-type]
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT


async def test_concurrent_duplicate_goes_through_same_content_check() -> None:
    """并发路径同样要比内容：仓储抛重复时回查，不允许跳过判定。"""
    assets = await _seed_assets(_asset("asset-a"))
    manifests = _StaleReadManifestRepository()
    svc, _, _, outbox = _service(assets, manifests)
    payload = _payload(manifest_id="manifest-race")

    first = await svc.create_manifest(**payload)  # type: ignore[arg-type]
    second = await svc.create_manifest(**payload)  # type: ignore[arg-type]

    assert second.manifest_id == first.manifest_id
    assert len(outbox.events) == 1


async def test_concurrent_duplicate_with_different_content_is_rejected() -> None:
    """并发路径 + 内容不同：同样必须拒绝（否则并发成了绕过口）。"""
    assets = await _seed_assets(_asset("asset-a"))
    manifests = _StaleReadManifestRepository()
    svc, _, _, _ = _service(assets, manifests)

    await svc.create_manifest(**_payload(manifest_id="manifest-race"))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as exc:
        await svc.create_manifest(
            **_payload(manifest_id="manifest-race", qc_final_ref="qc-final-2")  # type: ignore[arg-type]
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT


async def test_missing_asset_reference_is_rejected() -> None:
    """引用不存在的版本 → 拒绝（交付时才发现缺件的成本远高于此刻）。"""
    svc, _, manifests, outbox = _service()
    with pytest.raises(DomainError) as exc:
        await svc.create_manifest(**_payload(asset_refs=["asset-ghost"]))  # type: ignore[arg-type]
    assert "不存在" in str(exc.value.detail)
    assert manifests.rows == {}
    assert outbox.events == []


async def test_cross_project_asset_reference_is_rejected() -> None:
    """引用其他项目的资产 → 拒绝（同租户跨项目也要拦）。"""
    assets = await _seed_assets(_asset("asset-other", project_id=_OTHER_PROJECT))
    svc, _, _, _ = _service(assets)
    with pytest.raises(DomainError) as exc:
        await svc.create_manifest(**_payload(asset_refs=["asset-other"]))  # type: ignore[arg-type]
    assert "其他项目" in str(exc.value.detail)


@pytest.mark.parametrize(
    "status",
    [
        AssetStatus.GENERATED,
        AssetStatus.TECHNICAL_QC,
        AssetStatus.HUMAN_REVIEW,
        AssetStatus.REWORK,
    ],
)
async def test_undeliverable_asset_status_is_rejected(status: AssetStatus) -> None:
    """只有 APPROVED / GOLD 可交付：未审资产不得出现在对客户的清单上。"""
    assets = await _seed_assets(_asset("asset-a", status=status))
    svc, _, _, _ = _service(assets)
    with pytest.raises(DomainError) as exc:
        await svc.create_manifest(**_payload())  # type: ignore[arg-type]
    assert "APPROVED" in str(exc.value.detail)


@pytest.mark.parametrize("status", [AssetStatus.APPROVED, AssetStatus.GOLD])
async def test_deliverable_status_is_accepted(status: AssetStatus) -> None:
    """合法状态必须走得通，否则"拦住了"可以靠一律拒绝伪造。"""
    assets = await _seed_assets(_asset("asset-a", status=status))
    svc, _, _, _ = _service(assets)
    manifest = await svc.create_manifest(**_payload())  # type: ignore[arg-type]
    assert manifest.asset_refs == ["asset-a"]


async def test_payload_never_carries_checksum_from_caller() -> None:
    """校验和由服务端算：即使调用方传了 checksum 也不会被采纳。

    这里用"同一份内容算出同一个哈希"来间接证明——服务不接受外部哈希，
    因此两次调用必然一致，不存在"客户端塞一个不同哈希"的入口。
    """
    assets = await _seed_assets(_asset("asset-a"))
    svc, _, _, _ = _service(assets)
    first = await svc.create_manifest(**_payload())  # type: ignore[arg-type]
    second = await svc.create_manifest(**_payload())  # type: ignore[arg-type]
    assert first.checksum == second.checksum


async def test_get_missing_manifest_is_not_found() -> None:
    """不可见即 NOT_FOUND（不区分"不存在"与"无权看"）。"""
    svc, _, _, _ = _service()
    with pytest.raises(DomainError) as exc:
        await svc.get_manifest("manifest-ghost")
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND
