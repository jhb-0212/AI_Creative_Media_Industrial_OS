"""资产域应用服务单测（内存 fake 仓储 + fake outbox，无数据库）。

验证的是「用例规则」而不是 ORM：
- 幂等 / 覆盖拒绝 / 状态推进 / 职责分离 / 证据缺失拒绝 / 事件登记。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.versioning import ConcurrentModificationError
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.qc_report import QCReport
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.qc_repo import QCReportRepository
from services.asset_service.service.asset_service import AssetService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")
_SHA = "b" * 64


class FakeAssetRepository(AssetRepository):
    """内存仓储（存副本，才能真实暴露乐观锁比较逻辑）。"""

    def __init__(self) -> None:
        self.rows: dict[str, AssetVersion] = {}

    async def add(self, asset: AssetVersion) -> None:
        self.rows[asset.entity_id] = replace(asset)

    async def save(self, asset: AssetVersion, expected_version: int) -> None:
        stored = self.rows.get(asset.entity_id)
        if stored is None or stored.entity_version != expected_version:
            raise ConcurrentModificationError(asset.entity_id, expected_version)
        self.rows[asset.entity_id] = replace(asset)

    async def get_by_id(self, entity_id: str) -> AssetVersion | None:
        stored = self.rows.get(entity_id)
        return replace(stored) if stored is not None else None


class FakeQCRepository(QCReportRepository):
    def __init__(self) -> None:
        self.rows: dict[str, QCReport] = {}

    async def add(self, report: QCReport) -> None:
        self.rows[report.qc_report_id] = report

    async def get_by_id(self, qc_report_id: str) -> QCReport | None:
        return self.rows.get(qc_report_id)

    async def count_by_asset(self, asset_version_id: str) -> int:
        return sum(
            1 for r in self.rows.values() if r.asset_version_id == asset_version_id
        )


class FakeOutboxWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> uuid.UUID:
        event_id = uuid.uuid4()
        self.events.append(
            {
                "event_id": event_id,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return event_id


def _principal(subject: str, *roles: str) -> Principal:
    return Principal(
        subject_id=subject, tenant_id=_TENANT, roles=frozenset(roles)
    )


@pytest.fixture
def service() -> tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter]:
    assets = FakeAssetRepository()
    qc = FakeQCRepository()
    outbox = FakeOutboxWriter()
    return AssetService(assets, qc, outbox), assets, qc, outbox


async def _create(service: AssetService, **overrides: object) -> AssetVersion:
    params: dict[str, object] = {
        "asset_type": "IMAGE",
        "sha256": _SHA,
        "storage_ref": {"uri": "s3://bucket/key.png"},
        "source_asset_version_ids": [],
        "rights_manifest_ref": "rights-1",
        "adapter_id": "adapter-1",
        "tool_product": "tool-1",
        "operator_ref": None,
        "version": "1",
        "principal": _principal("worker-1", "Worker"),
    }
    params.update(overrides)
    return await service.create_version(**params)  # type: ignore[arg-type]


async def test_create_emits_event_and_starts_generated(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, assets, _, outbox = service
    asset = await _create(svc)
    assert asset.status is AssetStatus.GENERATED
    assert assets.rows[asset.entity_id].sha256 == asset.sha256
    assert [e["event_type"] for e in outbox.events] == ["ASSET_VERSION_CREATED"]


async def test_duplicate_same_content_is_idempotent(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, _, _, outbox = service
    first = await _create(svc, entity_id="fixed-id")
    second = await _create(svc, entity_id="fixed-id")
    assert first.entity_id == second.entity_id
    # 幂等返回不应产生第二个审计事件（重复提交不是新事实）。
    assert len(outbox.events) == 1


async def test_duplicate_different_content_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """覆盖既有版本是红线，必须拒绝（而不是静默改写）。"""
    svc, _, _, _ = service
    await _create(svc, entity_id="fixed-id")
    with pytest.raises(DomainError) as exc:
        await _create(svc, entity_id="fixed-id", sha256="c" * 64)
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT


async def test_qc_report_advances_asset_to_technical_qc(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, assets, qc, outbox = service
    asset = await _create(svc)
    updated, report = await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR", "score": 0.1}],
        principal=_principal("qc-1", "QC Worker"),
    )
    assert updated.status is AssetStatus.TECHNICAL_QC
    assert updated.entity_version == 2
    assert qc.rows[report.qc_report_id] is report
    assert assets.rows[asset.entity_id].status is AssetStatus.TECHNICAL_QC
    assert [e["event_type"] for e in outbox.events] == [
        "ASSET_VERSION_CREATED",
        "QC_REPORT_CREATED",
    ]


async def test_duplicate_qc_report_id_is_idempotent(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, _, qc, outbox = service
    asset = await _create(svc)
    first = await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
        qc_report_id="qc-fixed",
    )
    second = await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
        qc_report_id="qc-fixed",
    )
    assert second[1].qc_report_id == first[1].qc_report_id
    assert len(qc.rows) == 1
    assert [e["event_type"] for e in outbox.events].count("QC_REPORT_CREATED") == 1


async def test_duplicate_qc_report_id_across_assets_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """幂等键必须绑定资产：同一 report_id 提交给另一资产必须 409，不得静默继承。"""
    svc, assets, qc, outbox = service
    asset_a = await _create(svc)
    asset_b = await _create(svc, entity_id="asset-b", sha256="b" * 64)
    await svc.create_qc_report(
        asset_version_id=asset_a.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
        qc_report_id="shared-report-id",
    )
    with pytest.raises(DomainError) as exc:
        await svc.create_qc_report(
            asset_version_id=asset_b.entity_id,
            quality_profile_version="qp-1",
            detector_versions={"blur": "1.0"},
            findings=[{"code": "BLUR"}],
            principal=_principal("qc-1", "QC Worker"),
            qc_report_id="shared-report-id",
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
    assert len(qc.rows) == 1
    # B 不得因一次被拒绝的提交而前进状态，也不得多出第二个 QC 事件。
    assert assets.rows[asset_b.entity_id].status is AssetStatus.GENERATED
    assert [e["event_type"] for e in outbox.events].count("QC_REPORT_CREATED") == 1


async def test_approve_emits_one_event_per_hop(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """TECHNICAL_QC→HUMAN_REVIEW→APPROVED：两跳各发一条事件，不合并、不补记。"""
    svc, assets, _, outbox = service
    asset = await _create(svc)
    await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
    )
    approved = await svc.approve(
        asset_version_id=asset.entity_id, principal=_principal("director-1", "导演")
    )
    assert approved.status is AssetStatus.APPROVED
    # 两次迁移（TECHNICAL_QC→HUMAN_REVIEW→APPROVED）各推进一版。
    assert approved.entity_version == 4
    assert assets.rows[asset.entity_id].status is AssetStatus.APPROVED

    review_event, approve_event = outbox.events[-2], outbox.events[-1]
    assert review_event["event_type"] == "ASSET_VERSION_HUMAN_REVIEW_ENTERED"
    assert approve_event["event_type"] == "ASSET_VERSION_APPROVED"
    # 每条事件记录的是"相邻两状态"，审计链才能逐跳复算。
    assert review_event["payload"]["from_status"] == AssetStatus.TECHNICAL_QC.value
    assert review_event["payload"]["to_status"] == AssetStatus.HUMAN_REVIEW.value
    assert approve_event["payload"]["from_status"] == AssetStatus.HUMAN_REVIEW.value
    assert approve_event["payload"]["to_status"] == AssetStatus.APPROVED.value
    assert "human_review_in_same_call" not in approve_event["payload"]


async def test_approve_by_creator_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """职责分离红线：创建人不能自批。"""
    svc, _, _, _ = service
    asset = await _create(svc)
    await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
    )
    with pytest.raises(DomainError) as exc:
        await svc.approve(
            asset_version_id=asset.entity_id, principal=_principal("worker-1", "Worker")
        )
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


async def test_approve_without_qc_report_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, _, _, _ = service
    asset = await _create(svc)
    with pytest.raises(DomainError) as exc:
        await svc.approve(
            asset_version_id=asset.entity_id, principal=_principal("director-1", "导演")
        )
    # 未完成技术 QC（状态仍是 GENERATED）→ 状态拒绝，早于证据检查。
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


async def test_approve_without_rights_manifest_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """无权利证据不得批准。"""
    svc, _, _, _ = service
    asset = await _create(svc, rights_manifest_ref=None)
    await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
    )
    with pytest.raises(DomainError) as exc:
        await svc.approve(
            asset_version_id=asset.entity_id, principal=_principal("director-1", "导演")
        )
    assert "权利证据" in exc.value.detail


async def test_approve_missing_asset_returns_not_found(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, _, _, _ = service
    with pytest.raises(DomainError) as exc:
        await svc.approve(
            asset_version_id="nope", principal=_principal("director-1", "导演")
        )
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND


async def test_qc_report_on_approved_asset_is_rejected(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """批准后不再接受新报告（证据链冻结，避免事后补证据洗白）。"""
    svc, _, _, _ = service
    asset = await _create(svc)
    await svc.create_qc_report(
        asset_version_id=asset.entity_id,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        principal=_principal("qc-1", "QC Worker"),
    )
    await svc.approve(
        asset_version_id=asset.entity_id, principal=_principal("director-1", "导演")
    )
    with pytest.raises(DomainError):
        await svc.create_qc_report(
            asset_version_id=asset.entity_id,
            quality_profile_version="qp-1",
            detector_versions={"blur": "1.0"},
            findings=[{"code": "BLUR"}],
            principal=_principal("qc-1", "QC Worker"),
        )


async def test_read_version_returns_existing(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """READ 是纯读语义：返回既有信封，不推进版本号、不产生新事件。"""
    svc, _, _, outbox = service
    asset = await _create(svc)
    before_events = len(outbox.events)
    read_back = await svc.get_version(asset_version_id=asset.entity_id)
    assert read_back.entity_id == asset.entity_id
    assert read_back.entity_version == asset.entity_version
    assert len(outbox.events) == before_events


async def test_read_version_missing_is_not_found(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    svc, _, _, _ = service
    with pytest.raises(DomainError) as exc:
        await svc.get_version(asset_version_id="missing")
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND


class _ConcurrentAssetRepository(FakeAssetRepository):
    """模拟并发主键冲突的仓储：add 首次抛 IntegrityError，回查返回既有对象。

    用于验证「并发回查路径」与「快速路径」共用同一幂等判定——复核 R5。
    """

    def __init__(self, pre_existing: AssetVersion) -> None:
        super().__init__()
        self.rows[pre_existing.entity_id] = replace(pre_existing)
        self._add_failed_once = False

    async def add(self, asset: AssetVersion) -> None:
        if not self._add_failed_once:
            self._add_failed_once = True
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError(
                "INSERT", {}, Exception("duplicate key value violates unique constraint")
            )
        await super().add(asset)


def _concurrent_service(pre_existing: AssetVersion) -> tuple[
    AssetService, _ConcurrentAssetRepository, FakeQCRepository, FakeOutboxWriter
]:
    assets = _ConcurrentAssetRepository(pre_existing)
    qc = FakeQCRepository()
    outbox = FakeOutboxWriter()
    return AssetService(assets, qc, outbox), assets, qc, outbox


async def test_concurrent_same_content_is_idempotent() -> None:
    """并发主键冲突回查：sha256 且 asset_type 一致 → 幂等返回既有对象（R5）。"""
    pre = AssetVersion(
        entity_id="concurrent-id",
        created_by="worker-1",
        tenant_id=_TENANT,
        asset_type="IMAGE",
        sha256=_SHA,
        storage_ref={"uri": "s3://bucket/key.png"},
    )
    svc, _, _, outbox = _concurrent_service(pre)

    result = await svc.create_version(
        asset_type="IMAGE",
        sha256=_SHA,
        storage_ref={"uri": "s3://bucket/key.png"},
        source_asset_version_ids=[],
        rights_manifest_ref=None,
        adapter_id=None,
        tool_product=None,
        operator_ref=None,
        version=None,
        principal=_principal("worker-1", "Worker"),
        entity_id="concurrent-id",
    )
    # 幂等返回既有对象，且不产生第二个事件。
    assert result.entity_id == "concurrent-id"
    assert len(outbox.events) == 0


async def test_concurrent_same_sha_different_type_is_rejected() -> None:
    """并发回查：sha256 相同但 asset_type 不同 → 拒绝（不得静默返回旧对象）。

    这是 R5 的核心：快速路径会因 asset_type 不一致而 409，并发回查路径也必须
    同样拒绝，否则同一业务规则在竞态下出现两种结果。
    """
    pre = AssetVersion(
        entity_id="concurrent-id",
        created_by="worker-1",
        tenant_id=_TENANT,
        asset_type="VIDEO",
        sha256=_SHA,
        storage_ref={"uri": "s3://bucket/key.mp4"},
    )
    svc, _, _, _ = _concurrent_service(pre)

    with pytest.raises(DomainError) as exc:
        await svc.create_version(
            asset_type="AUDIO",  # 同 sha、不同 asset_type
            sha256=_SHA,
            storage_ref={"uri": "s3://bucket/key.mp4"},
            source_asset_version_ids=[],
            rights_manifest_ref=None,
            adapter_id=None,
            tool_product=None,
            operator_ref=None,
            version=None,
            principal=_principal("worker-1", "Worker"),
            entity_id="concurrent-id",
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
