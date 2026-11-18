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
    APPROVE_ACTION,
    ASSET_ALLOWED_TRANSITIONS,
    ASSET_TRANSITIONS,
    QC_REPORT_ACTION,
    STATUS_CHANGE_ACTION,
    can_transition_asset,
)

_SHA = "a" * 64
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _asset(**overrides: object) -> AssetVersion:
    payload: dict[str, object] = {
        "entity_id": "asset-1",
        "created_by": "worker-1",
        "tenant_id": _TENANT,
        "project_id": "project-1",
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
    asset.transition_to(AssetStatus.TECHNICAL_QC, "qc", QC_REPORT_ACTION)
    asset.transition_to(AssetStatus.HUMAN_REVIEW, "review", APPROVE_ACTION)
    asset.transition_to(AssetStatus.APPROVED, "approve", APPROVE_ACTION)
    asset.transition_to(AssetStatus.GOLD, "gold", STATUS_CHANGE_ACTION)
    assert asset.status is AssetStatus.GOLD
    assert asset.entity_version == 5


def test_side_branch_is_not_enterable() -> None:
    """旁支（例如此处模拟 REJECTED 语义）不在转移表里 —— 未登记即拒绝。

    资产枚举当前只有主链五态；这里用「GENERATED 直接跳到 APPROVED」验证
    跳关同样被拒绝，证明拒绝不是靠枚举缺值，而是靠转移表判定。
    """
    asset = _asset()
    with pytest.raises(DomainError):
        asset.transition_to(AssetStatus.APPROVED, "skip", STATUS_CHANGE_ACTION)


def test_gold_is_terminal() -> None:
    """GOLD 无出边：任何回退/复活一律拒绝。"""
    asset = _asset()
    for target, ref, action in (
        (AssetStatus.TECHNICAL_QC, "qc", QC_REPORT_ACTION),
        (AssetStatus.HUMAN_REVIEW, "review", APPROVE_ACTION),
        (AssetStatus.APPROVED, "approve", APPROVE_ACTION),
        (AssetStatus.GOLD, "gold", STATUS_CHANGE_ACTION),
    ):
        asset.transition_to(target, ref, action)
    with pytest.raises(DomainError):
        asset.transition_to(AssetStatus.APPROVED, "revive", STATUS_CHANGE_ACTION)
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
    ("source", "target"),
    [(edge.source, edge.target) for edge in ASSET_TRANSITIONS],
    ids=[edge.label for edge in ASSET_TRANSITIONS],
)
def test_edge_ownership_is_enforced_by_aggregate(
    source: AssetStatus, target: AssetStatus
) -> None:
    """每条边只对它自己的动作可达（N-07）。

    这是「所有权被记录」与「所有权被执行」的分界线：整改前 ``owner_action``
    只是写进审计事实的一个字符串，任何调用方都能带着它走任意已登记的边。
    现在聚合根的 ``transition_to`` 自己判定——不依赖服务层「记得先查」，
    因此这条测试直接对聚合调用，绕过全部服务层代码。

    夹具用 ``_asset(status=source)`` 构造一个「已在 source 态」的新对象，
    而不是把旧对象的状态字段改写：后者正是断言 ``data.transition_single_entry``
    禁止的写法，测试自己也不该示范。
    """
    asset = _asset(status=source)
    owner = next(e.owner_action for e in ASSET_TRANSITIONS if e.key == (source, target))
    foreign = next(
        action
        for action in (QC_REPORT_ACTION, APPROVE_ACTION, STATUS_CHANGE_ACTION)
        if action != owner
    )

    with pytest.raises(DomainError) as denied:
        asset.transition_to(target, "ref", foreign)
    assert "独占" in str(denied.value)
    # 拒绝必须是「没发生」：状态与版本号都不动，否则拒绝只挡住了返回值。
    assert asset.status is source
    assert asset.entity_version == 1


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


def test_asset_requires_project_ownership() -> None:
    """无项目归属的资产不得构造（SCOPE-3：无归属即无法授权）。"""
    with pytest.raises(DomainError):
        _asset(project_id="")


def test_asset_serialization_hides_project_id() -> None:
    """归属键与 tenant_id 同待遇：只用于服务端判定，不下发响应。"""
    data = _asset().to_dict()
    assert "project_id" not in data
    assert "tenant_id" not in data
