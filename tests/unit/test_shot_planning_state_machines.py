"""镜头域 1C 状态机与领域规则单测（纯领域层，无数据库依赖）。

覆盖验收清单 D1（未登记转移一律拒绝）+ C3（ConstraintPack 硬门语义）：
- ConstraintPack：7.9 权威状态机全边（DRAFT→ACTIVE；ACTIVE→STALE/SUPERSEDED/
  REVOKED）+ 非法转移拒绝 + NOT_APPLICABLE 语义（不得携带锁定维度/约束）；
- ShotVisualPlan：C.7 状态机全边 + 非法转移拒绝 + 逐字段授权锁定语义；
- ShotSpec：锁定维度词典校验（未登记维度拒绝）。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.shot_planning.domain.constraint_pack import ConstraintPack
from services.shot_planning.domain.enums import (
    LOCKABLE_DIMENSIONS,
    ConstraintPackApplicability,
    ConstraintPackStatus,
    ShotVisualPlanStatus,
)
from services.shot_planning.domain.shot_spec import ShotSpec
from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan
from services.shot_planning.domain.state_machine import (
    CONSTRAINT_PACK_ALLOWED_TRANSITIONS,
    SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS,
    assert_transition_constraint_pack,
    assert_transition_visual_plan,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")

_CONSTRAINT_ENTRY = {
    "constraint_id": "c_001",
    "target": "char_001",
    "shot_scope": "SHOT_001..SHOT_003",
    "rule": "服装/伤口状态连续",
    "source": "SCRIPT",
    "authority_level": "HARD",
    "valid_at": "2026-11-17T00:00:00Z",
    "approval_ref": "approval_001",
}


def _make_pack(
    applicability: ConstraintPackApplicability = ConstraintPackApplicability.APPLICABLE,
) -> ConstraintPack:
    return ConstraintPack(
        entity_id=uuid.uuid4().hex,
        project_id="proj_001",
        shot_ids=["shot_001", "shot_002"],
        applicability=applicability,
        locked_dimensions=["camera_path"] if applicability is ConstraintPackApplicability.APPLICABLE else [],
        constraints=[dict(_CONSTRAINT_ENTRY)] if applicability is ConstraintPackApplicability.APPLICABLE else [],
        created_by="user_001",
        tenant_id=_TENANT,
    )


def _make_plan() -> ShotVisualPlan:
    return ShotVisualPlan(
        entity_id=uuid.uuid4().hex,
        shot_id="shot_001",
        project_id="proj_001",
        intent_summary="角色 A 情绪转折",
        camera_language="手持跟随",
        shot_size="MEDIUM",
        lens_range="35-50mm",
        motion_intent="跟随角色移动",
        lighting_intent="低照度暖光",
        open_fields=["composition_notes", "color_mood"],
        created_by="agent_001",
        tenant_id=_TENANT,
    )


def _make_spec() -> ShotSpec:
    return ShotSpec(
        entity_id=uuid.uuid4().hex,
        shot_revision_id=uuid.uuid4().hex,
        shot_id="shot_001",
        project_id="proj_001",
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "DOLLY", "keyframes": []},
        created_by="user_001",
        tenant_id=_TENANT,
    )


# --- ConstraintPack 状态机（D1）------------------------------------------


def test_constraint_pack_full_legal_paths() -> None:
    """7.9 全部合法边可走通：DRAFT→ACTIVE→{STALE,SUPERSEDED,REVOKED}。"""
    expected_edges = {
        (ConstraintPackStatus.DRAFT, ConstraintPackStatus.ACTIVE),
        (ConstraintPackStatus.ACTIVE, ConstraintPackStatus.STALE),
        (ConstraintPackStatus.ACTIVE, ConstraintPackStatus.SUPERSEDED),
        (ConstraintPackStatus.ACTIVE, ConstraintPackStatus.REVOKED),
    }
    in_code = {
        (src, tgt)
        for src, targets in CONSTRAINT_PACK_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert in_code == expected_edges

    # 主路径：DRAFT -> ACTIVE。
    pack = _make_pack()
    pack.activate(approved_by="director_001", audit_ref="lock:1")
    assert pack.status is ConstraintPackStatus.ACTIVE
    assert pack.approved_by == "director_001"
    assert pack.approved_at is not None

    # 终态分支各自可走通。
    for target, method in (
        (ConstraintPackStatus.STALE, "mark_stale"),
        (ConstraintPackStatus.SUPERSEDED, "supersede"),
        (ConstraintPackStatus.REVOKED, "revoke"),
    ):
        p = _make_pack()
        p.activate(approved_by="director_001", audit_ref="lock:1")
        getattr(p, method)(audit_ref="edge")
        assert p.status is target


def test_constraint_pack_illegal_transitions_denied() -> None:
    """未登记转移一律拒绝（D1）：跳过 ACTIVE、终态复活、DRAFT 直达终态。"""
    illegal = [
        (ConstraintPackStatus.DRAFT, ConstraintPackStatus.STALE),
        (ConstraintPackStatus.DRAFT, ConstraintPackStatus.SUPERSEDED),
        (ConstraintPackStatus.DRAFT, ConstraintPackStatus.REVOKED),
        (ConstraintPackStatus.ACTIVE, ConstraintPackStatus.ACTIVE),
        (ConstraintPackStatus.STALE, ConstraintPackStatus.ACTIVE),
        (ConstraintPackStatus.SUPERSEDED, ConstraintPackStatus.ACTIVE),
        (ConstraintPackStatus.REVOKED, ConstraintPackStatus.ACTIVE),
    ]
    for src, tgt in illegal:
        assert tgt not in CONSTRAINT_PACK_ALLOWED_TRANSITIONS.get(src, frozenset())
        with pytest.raises(DomainError) as exc_info:
            assert_transition_constraint_pack(src, tgt)
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"

    # 终态不可原地复活：聚合根方法同样拒绝。
    pack = _make_pack()
    pack.activate(approved_by="director_001", audit_ref="lock:1")
    pack.revoke(audit_ref="edge")
    with pytest.raises(DomainError):
        pack.activate(approved_by="director_001", audit_ref="revive")


def test_constraint_pack_not_applicable_semantics() -> None:
    """C3：NOT_APPLICABLE 包不得携带锁定维度/约束；APPLICABLE 包条目必填。"""
    with pytest.raises(DomainError) as exc_info:
        _make_pack(ConstraintPackApplicability.APPLICABLE).__class__(
            entity_id=uuid.uuid4().hex,
            project_id="proj_001",
            shot_ids=["shot_001"],
            applicability=ConstraintPackApplicability.NOT_APPLICABLE,
            locked_dimensions=["camera_path"],
            created_by="user_001",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    # NOT_APPLICABLE 干净包可正常构造并经 ACTIVE（ADR-088：不得作为默认值绕过）。
    clean = _make_pack(ConstraintPackApplicability.NOT_APPLICABLE)
    assert clean.locked_dimensions == []
    clean.activate(approved_by="director_001", audit_ref="lock:1")
    assert clean.status is ConstraintPackStatus.ACTIVE


def test_constraint_pack_entry_required_keys() -> None:
    """APPLICABLE 包约束条目必须齐备（7.9 条目内全部必填）。"""
    with pytest.raises(DomainError) as exc_info:
        ConstraintPack(
            entity_id=uuid.uuid4().hex,
            project_id="proj_001",
            shot_ids=["shot_001"],
            applicability=ConstraintPackApplicability.APPLICABLE,
            constraints=[{"constraint_id": "c_001"}],
            created_by="user_001",
            tenant_id=_TENANT,
        )
    assert "approval_ref" in str(exc_info.value.detail)
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    # 未登记维度拒绝。
    with pytest.raises(DomainError):
        ConstraintPack(
            entity_id=uuid.uuid4().hex,
            project_id="proj_001",
            shot_ids=["shot_001"],
            applicability=ConstraintPackApplicability.APPLICABLE,
            locked_dimensions=["makeup"],
            constraints=[dict(_CONSTRAINT_ENTRY)],
            created_by="user_001",
            tenant_id=_TENANT,
        )


def test_lockable_dimensions_registry() -> None:
    """锁定维度词典与 7.9 契约一致（7 个维度）。"""
    assert frozenset(
        {
            "camera_intrinsics",
            "camera_extrinsics",
            "camera_path",
            "blocking",
            "shot_size_nodes",
            "cut_points",
            "spatial_relations",
        }
    ) == LOCKABLE_DIMENSIONS


# --- ShotVisualPlan 状态机（D1）------------------------------------------


def test_visual_plan_full_legal_paths() -> None:
    """C.7 主路径 + 锁定分支可走通。"""
    in_code = {
        (src, tgt)
        for src, targets in SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert in_code == {
        (ShotVisualPlanStatus.DRAFT, ShotVisualPlanStatus.REVIEW),
        (ShotVisualPlanStatus.REVIEW, ShotVisualPlanStatus.APPROVED),
        (ShotVisualPlanStatus.REVIEW, ShotVisualPlanStatus.REJECTED),
        (ShotVisualPlanStatus.APPROVED, ShotVisualPlanStatus.PARTIALLY_LOCKED),
        (ShotVisualPlanStatus.APPROVED, ShotVisualPlanStatus.LOCKED),
        (ShotVisualPlanStatus.PARTIALLY_LOCKED, ShotVisualPlanStatus.LOCKED),
    }

    # 主路径：DRAFT → REVIEW → APPROVED → 部分锁定 → 全锁。
    plan = _make_plan()
    plan.submit(audit_ref="s")
    assert plan.status is ShotVisualPlanStatus.REVIEW
    plan.approve(audit_ref="a")
    assert plan.status is ShotVisualPlanStatus.APPROVED

    plan.lock_fields(
        field_names=["composition_notes"],
        locked_by="director_001",
        reason="构图定稿",
        audit_ref="l1",
    )
    assert plan.status is ShotVisualPlanStatus.PARTIALLY_LOCKED
    assert plan.open_fields == ["color_mood"]
    entry = plan.locked_fields[0]
    assert entry["field"] == "composition_notes"
    assert entry["locked_by"] == "director_001"
    assert entry["reason"] == "构图定稿"
    assert entry["version"] >= 1

    plan.lock_fields(
        field_names=["color_mood"],
        locked_by="director_001",
        reason="色调定稿",
        audit_ref="l2",
    )
    assert plan.status is ShotVisualPlanStatus.LOCKED
    assert plan.open_fields == []

    # 一次性全锁：APPROVED → LOCKED 直达。
    plan2 = _make_plan()
    plan2.submit(audit_ref="s")
    plan2.approve(audit_ref="a")
    plan2.lock_fields(
        field_names=["composition_notes", "color_mood"],
        locked_by="director_001",
        reason="定稿",
        audit_ref="l",
    )
    assert plan2.status is ShotVisualPlanStatus.LOCKED


def test_visual_plan_illegal_transitions_denied() -> None:
    """未登记转移一律拒绝（D1）：跳审、终态复活、锁定态回退。"""
    illegal = [
        (ShotVisualPlanStatus.DRAFT, ShotVisualPlanStatus.APPROVED),
        (ShotVisualPlanStatus.DRAFT, ShotVisualPlanStatus.REJECTED),
        (ShotVisualPlanStatus.REVIEW, ShotVisualPlanStatus.LOCKED),
        (ShotVisualPlanStatus.APPROVED, ShotVisualPlanStatus.REVIEW),
        (ShotVisualPlanStatus.PARTIALLY_LOCKED, ShotVisualPlanStatus.APPROVED),
        (ShotVisualPlanStatus.REJECTED, ShotVisualPlanStatus.REVIEW),
        (ShotVisualPlanStatus.LOCKED, ShotVisualPlanStatus.PARTIALLY_LOCKED),
    ]
    for src, tgt in illegal:
        with pytest.raises(DomainError) as exc_info:
            assert_transition_visual_plan(src, tgt)
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


def test_visual_plan_lock_requires_approved_state() -> None:
    """锁定只发生在 APPROVED/PARTIALLY_LOCKED 态；DRAFT/REVIEW/REJECTED 拒绝。"""
    for setup in ("draft", "review", "rejected"):
        plan = _make_plan()
        if setup == "review":
            plan.submit(audit_ref="s")
        elif setup == "rejected":
            plan.submit(audit_ref="s")
            plan.reject(audit_ref="r")
        with pytest.raises(DomainError) as exc_info:
            plan.lock_fields(
                field_names=["color_mood"],
                locked_by="director_001",
                reason="理由",
                audit_ref="l",
            )
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


def test_visual_plan_lock_unknown_field_denied() -> None:
    """锁定不在开放字段集合内的字段拒绝（逐字段授权，19.2）。"""
    plan = _make_plan()
    plan.submit(audit_ref="s")
    plan.approve(audit_ref="a")
    with pytest.raises(DomainError) as exc_info:
        plan.lock_fields(
            field_names=["nonexistent_field"],
            locked_by="director_001",
            reason="理由",
            audit_ref="l",
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


# --- ShotSpec 领域规则 ----------------------------------------------------


def test_shot_spec_lock_dimensions() -> None:
    """SHOTSPEC.LOCK 领域规则：登记维度可锁、未登记维度拒绝、依据必填。"""
    spec = _make_spec()
    spec.lock_dimensions(
        dimensions=["camera_path", "cut_points"],
        locked_by="director_001",
        rationale="运镜与切点经导演确认",
        audit_ref="SHOTSPEC.LOCK:1",
    )
    assert spec.locked_dimensions == ["camera_path", "cut_points"]
    assert spec.locked_by == "director_001"
    assert spec.lock_rationale == "运镜与切点经导演确认"
    assert spec.entity_version == 2

    with pytest.raises(DomainError) as exc_info:
        spec.lock_dimensions(
            dimensions=["makeup"],
            locked_by="director_001",
            rationale="未登记维度",
            audit_ref="x",
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    with pytest.raises(DomainError):
        spec.lock_dimensions(
            dimensions=["camera_path"],
            locked_by="director_001",
            rationale="  ",
            audit_ref="x",
        )


def test_shot_spec_content_immutable_by_construction() -> None:
    """规格内容列（duration_ms/切点等）构造后无改写方法（C2：变更=新建）。"""
    spec = _make_spec()
    public_mutators = [
        name
        for name in dir(spec)
        if not name.startswith("_")
        and callable(getattr(spec, name))
        and name not in {"lock_dimensions", "to_dict"}
    ]
    assert public_mutators == [], (
        f"ShotSpec 出现非预期公开方法: {public_mutators}（内容不可原地改写）"
    )
