"""DeliveryManifest 领域对象单测（纯函数，无数据库）。

本文件要证明的是「交付清单的三条结构性性质」，而不是字段齐全：

1. **哈希只覆盖可重建内容**：同一组引用 + 同一份配方必须算出同一个哈希，
   且与引用书写顺序无关。否则 G1「用清单重建一次交付，哈希应与原件一致」
   永远无法成立——顺序、字典插入顺序、时间戳都会让哈希漂移。
2. **缺证据的清单不可存在**：`asset_refs` / `rights_evidence_refs` /
   `rebuild_recipe` 任一为空即拒绝。清单是对客户的交付依据，缺权利证据
   的清单一旦成立，风险直接出到客户侧。
3. **CREATE 只产出 DRAFT，状态只能沿登记边推进**：`DELIVERY.MANIFEST.CREATE`
   固定生成 DRAFT；构造期允许读出已推进的状态（否则只读路径会崩），但状态变化
   只能经 `transition_to()` 沿状态机合法边进行，非法跳转被拒（D4）。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.asset_service.domain.delivery_manifest import (
    DeliveryManifest,
    compute_manifest_checksum,
)
from services.asset_service.domain.enums import DeliveryManifestStatus

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _manifest(**overrides: object) -> DeliveryManifest:
    params: dict[str, object] = {
        "manifest_id": "manifest-1",
        "project_id": "proj-delivery",
        "asset_refs": ["asset-a", "asset-b"],
        "spec_ref": "spec-1",
        "qc_final_ref": "qc-final-1",
        "picture_lock_ref": "picture-lock-1",
        "rights_evidence_refs": ["rights-1"],
        "rebuild_recipe": "recipe://spec-1/v1",
        "created_by": "delivery_1",
        "tenant_id": _TENANT,
    }
    params.update(overrides)
    return DeliveryManifest(**params)  # type: ignore[arg-type]


def test_checksum_ignores_reference_order() -> None:
    """引用书写顺序不是内容语义：同一交付只应有一个哈希。"""
    forward = _manifest(asset_refs=["asset-a", "asset-b"])
    backward = _manifest(asset_refs=["asset-b", "asset-a"])
    assert forward.checksum == backward.checksum


def test_recipe_text_is_hashed_verbatim_without_normalisation() -> None:
    """配方是不透明字符串，平台不改写它——**包括不去裁剪空白**。

    这是契约类型（string）带来的取舍，必须测出来而不是写进注释就算：
    "同一份交付算出同一哈希"要求配方字符串本身确定，这是产出方的责任。
    平台能做且已做的是：改一个字就换一个哈希，因此"配方被悄悄改过"不可能
    带着同一个校验和发出去。
    """
    exact = _manifest(rebuild_recipe="recipe://spec-1/v1")
    padded = _manifest(rebuild_recipe="recipe://spec-1/v1 ")
    assert exact.checksum != padded.checksum


def test_checksum_changes_with_rebuild_recipe() -> None:
    """配方变了 = 产物可能不同，哈希必须跟着变。

    这是「哈希一致但产物不同」这类最难发现的交付缺陷的拦截点：只比对引用
    集合会漏掉"按什么顺序、什么规格拼"的差异。
    """
    base = _manifest(rebuild_recipe="recipe://spec-1/v1")
    changed = _manifest(rebuild_recipe="recipe://spec-1/v2")
    assert base.checksum != changed.checksum


def test_checksum_excludes_manifest_id_and_timestamps() -> None:
    """manifest_id 与时间戳不进哈希（重建时必然不同，纳入即让 G1 无法成立）。"""
    first = _manifest(manifest_id="manifest-1")
    second = _manifest(manifest_id="manifest-2")
    assert first.checksum == second.checksum


def test_checksum_is_stable_hex_digest() -> None:
    """哈希形态固定为 64 位十六进制（下游按定长字符串存储/比对）。"""
    checksum = compute_manifest_checksum(
        asset_refs=["asset-a"],
        spec_ref="spec-1",
        qc_final_ref="qc-final-1",
        picture_lock_ref="picture-lock-1",
        rights_evidence_refs=["rights-1"],
        rebuild_recipe="recipe://spec-1/v1",
    )
    assert len(checksum) == 64
    assert set(checksum) <= set("0123456789abcdef")


def test_rebuild_projection_matches_original_checksum() -> None:
    """G1：用清单内容重建一次，投影的哈希必须与原件一致。"""
    manifest = _manifest()
    projection = manifest.rebuild_projection()
    recomputed = compute_manifest_checksum(
        asset_refs=list(projection["asset_refs"]),  # type: ignore[arg-type]
        spec_ref=str(projection["spec_ref"]),
        qc_final_ref=str(projection["qc_final_ref"]),
        picture_lock_ref=str(projection["picture_lock_ref"]),
        rights_evidence_refs=list(projection["rights_evidence_refs"]),  # type: ignore[arg-type]
        rebuild_recipe=str(projection["rebuild_recipe"]),
    )
    assert recomputed == manifest.checksum


@pytest.mark.parametrize(
    ("field", "detail_keyword"),
    [
        ("asset_refs", "asset_refs"),
        ("rights_evidence_refs", "rights_evidence_refs"),
        ("rebuild_recipe", "rebuild_recipe"),
    ],
)
def test_empty_evidence_is_rejected(field: str, detail_keyword: str) -> None:
    """三类证据任一为空 → 拒绝（空清单不是"待补充"，是无效清单）。"""
    with pytest.raises(DomainError) as exc:
        _manifest(**{field: [] if field != "rebuild_recipe" else ""})
    assert detail_keyword in str(exc.value.detail)


@pytest.mark.parametrize(
    "field", ["manifest_id", "spec_ref", "qc_final_ref", "picture_lock_ref", "project_id"]
)
def test_required_reference_must_not_be_blank(field: str) -> None:
    """必填引用不得为空白串（空串会让"引用了证据"变成一句空话）。"""
    with pytest.raises(DomainError):
        _manifest(**{field: "   "})


def test_illegal_status_jump_is_rejected() -> None:
    """状态只能沿登记边推进：DRAFT 不能直接跳 VALIDATED/RELEASED（31.5）。"""
    manifest = _manifest()
    with pytest.raises(DomainError) as exc:
        manifest.transition_to(
            DeliveryManifestStatus.VALIDATED, audit_ref="event-1"
        )
    assert "DRAFT" in str(exc.value.detail) and "VALIDATED" in str(exc.value.detail)


def test_transition_requires_authoritative_audit_ref() -> None:
    """每次状态推进都必须带权威依据（事件 id），否则状态无来源可追。"""
    with pytest.raises(DomainError):
        _manifest().transition_to(DeliveryManifestStatus.VALIDATING, audit_ref="  ")


def test_transition_is_immutable_and_bumps_version() -> None:
    """推进返回新对象：内容不变、状态推进、版本递增、audit_ref 记录来源。"""
    original = _manifest()
    advanced = original.transition_to(
        DeliveryManifestStatus.VALIDATING, audit_ref="event-1"
    )
    assert original.status is DeliveryManifestStatus.DRAFT
    assert original.entity_version == 1
    assert advanced.status is DeliveryManifestStatus.VALIDATING
    assert advanced.entity_version == 2
    assert advanced.audit_ref == "event-1"
    assert advanced.checksum == original.checksum  # 内容哈希不变


def test_to_dict_omits_private_columns() -> None:
    """响应形态不得泄漏 project_id / tenant_id（契约未声明，属私有隔离列）。"""
    payload = _manifest().to_dict()
    assert "project_id" not in payload
    assert "tenant_id" not in payload
    assert payload["entity_id"] == payload["manifest_id"]
    assert payload["entity_type"] == "DeliveryManifest"


def test_to_dict_declares_no_gate_pass_field() -> None:
    """C6「不声明未发生的 Gate PASS」由字段集缺项保证，不是靠代码自觉。

    结构性证明：清单只有证据引用（qc_final_ref / picture_lock_ref /
    rights_evidence_refs），没有任何布尔型 gate_pass 类字段可写。
    """
    payload = _manifest().to_dict()
    gate_like = [key for key in payload if "gate" in key.lower() and "pass" in key.lower()]
    assert gate_like == []
    assert isinstance(payload["qc_final_ref"], str)
