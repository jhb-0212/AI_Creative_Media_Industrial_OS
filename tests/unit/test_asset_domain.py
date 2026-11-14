"""资产域领域规则单测（纯内存，无数据库）。

覆盖：
- 状态机只允许契约主链，旁支与跳关一律拒绝（31.5）。
- 资产最低可用性：sha256 形状、storage_ref 非空。
- QC 报告「缺证据不得存在」。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.qc_report import QCReport
from services.asset_service.domain.state_machine import (
    ASSET_ALLOWED_TRANSITIONS,
    can_transition_asset,
)

_SHA = "a" * 64
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _asset(**overrides: object) -> AssetVersion:
    payload: dict[str, object] = {
        "entity_id": "asset-1",
        "created_by": "worker-1",
        "tenant_id": _TENANT,
        "asset_type": "IMAGE",
        "sha256": _SHA,
        "storage_ref": {"uri": "s3://bucket/key.png"},
    }
    payload.update(overrides)
    return AssetVersion(**payload)  # type: ignore[arg-type]


def test_legal_chain_advances_version() -> None:
    """契约主链可逐跳推进，每跳 entity_version +1。"""
    asset = _asset()
    assert asset.status is AssetStatus.GENERATED
    asset.transition_to(AssetStatus.TECHNICAL_QC, "qc")
    asset.transition_to(AssetStatus.HUMAN_REVIEW, "review")
    asset.transition_to(AssetStatus.APPROVED, "approve")
    asset.transition_to(AssetStatus.GOLD, "gold")
    assert asset.status is AssetStatus.GOLD
    assert asset.entity_version == 5


def test_side_branch_is_not_enterable() -> None:
    """旁支（例如此处模拟 REJECTED 语义）不在转移表里 —— 未登记即拒绝。

    资产枚举当前只有主链五态；这里用「GENERATED 直接跳到 APPROVED」验证
    跳关同样被拒绝，证明拒绝不是靠枚举缺值，而是靠转移表判定。
    """
    asset = _asset()
    with pytest.raises(DomainError):
        asset.transition_to(AssetStatus.APPROVED, "skip")


def test_gold_is_terminal() -> None:
    """GOLD 无出边：任何回退/复活一律拒绝。"""
    asset = _asset()
    for target, ref in (
        (AssetStatus.TECHNICAL_QC, "qc"),
        (AssetStatus.HUMAN_REVIEW, "review"),
        (AssetStatus.APPROVED, "approve"),
        (AssetStatus.GOLD, "gold"),
    ):
        asset.transition_to(target, ref)
    with pytest.raises(DomainError):
        asset.transition_to(AssetStatus.APPROVED, "revive")
    assert ASSET_ALLOWED_TRANSITIONS[AssetStatus.GOLD] == frozenset()


def test_transition_table_matches_contract_shape() -> None:
    """转移表规模固定为契约解析出的 4 条（防止悄悄扩边）。"""
    edges = {
        (src.value, dst.value)
        for src, targets in ASSET_ALLOWED_TRANSITIONS.items()
        for dst in targets
    }
    assert edges == {
        ("GENERATED", "TECHNICAL_QC"),
        ("TECHNICAL_QC", "HUMAN_REVIEW"),
        ("HUMAN_REVIEW", "APPROVED"),
        ("APPROVED", "GOLD"),
    }


def test_can_transition_helper_semantics() -> None:
    assert can_transition_asset(AssetStatus.GENERATED, AssetStatus.TECHNICAL_QC)
    assert not can_transition_asset(AssetStatus.GENERATED, AssetStatus.APPROVED)


@pytest.mark.parametrize(
    "bad_sha",
    ["", "abc", "g" * 64, "a" * 63, "a" * 65],
)
def test_sha256_shape_is_enforced(bad_sha: str) -> None:
    with pytest.raises(DomainError):
        _asset(sha256=bad_sha)


def test_sha256_is_normalized_lowercase() -> None:
    asset = _asset(sha256="A" * 64)
    assert asset.sha256 == "a" * 64


def test_empty_storage_ref_rejected() -> None:
    with pytest.raises(DomainError):
        _asset(storage_ref={})


def test_empty_asset_type_rejected() -> None:
    with pytest.raises(DomainError):
        _asset(asset_type=" ")


def test_asset_serializes_contract_envelope() -> None:
    asset = _asset(rights_manifest_ref="rights-1")
    data = asset.to_dict()
    assert data["asset_version_id"] == asset.entity_id
    assert data["status"] == "GENERATED"
    assert data["owner_module"] == "asset-service"
    assert data["rights_manifest_ref"] == "rights-1"


def test_qc_report_requires_findings() -> None:
    """缺证据报告不得存在（红线：空报告不得 PASS）。"""
    with pytest.raises(DomainError):
        QCReport(
            qc_report_id="qc-1",
            asset_version_id="asset-1",
            tenant_id=_TENANT,
            quality_profile_version="qp-1",
            detector_versions={"blur": "1.0"},
            findings=[],
            created_by="qc-worker",
        )


def test_qc_report_requires_detector_versions() -> None:
    with pytest.raises(DomainError):
        QCReport(
            qc_report_id="qc-1",
            asset_version_id="asset-1",
            tenant_id=_TENANT,
            quality_profile_version="qp-1",
            detector_versions={},
            findings=[{"code": "BLUR"}],
            created_by="qc-worker",
        )


def test_qc_report_serializes_enums_as_strings() -> None:
    from services.asset_service.domain.enums import DeviationClass

    report = QCReport(
        qc_report_id="qc-1",
        asset_version_id="asset-1",
        tenant_id=_TENANT,
        quality_profile_version="qp-1",
        detector_versions={"blur": "1.0"},
        findings=[{"code": "BLUR"}],
        created_by="qc-worker",
        deviation_class=DeviationClass.INTENTIONAL,
    )
    data = report.to_dict()
    assert data["deviation_class"] == "INTENTIONAL"
    assert data["intent_match_status"] is None
