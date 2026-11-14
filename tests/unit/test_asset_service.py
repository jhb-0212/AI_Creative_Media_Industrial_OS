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


async def test_approve_walks_human_review_step(
    service: tuple[AssetService, FakeAssetRepository, FakeQCRepository, FakeOutboxWriter],
) -> None:
    """TECHNICAL_QC 批准时必须先落 HUMAN_REVIEW 再 APPROVED，不跳关。"""
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
    event = outbox.events[-1]
    assert event["event_type"] == "ASSET_VERSION_APPROVED"
    assert event["payload"]["human_review_in_same_call"] is True  # type: ignore[index]


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
