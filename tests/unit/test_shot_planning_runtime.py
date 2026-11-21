"""Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 领域不变量测试。

这一批测的是「契约写的约束在运行时真的成立」：
- 未登记转移与越权动作被拒（31.5 / N-07）；
- 终态不可原地复活（红线）；
- Shot 的 6 条守卫边在缺字段时拒绝，且拒绝发生在领域层（不是等库层 CHECK）；
- ReworkTask 的 REJECTED 无入边：合法状态但没有任何授权入口；
- 复核人不得为返工执行人本人（7.6 职责分离）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.enums import (
    ReworkTaskStatus,
    SceneStatus,
    ShotDraftStatus,
    ShotStatus,
)
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.state_machine import (
    REWORK_TASK_STATUS_CHANGE_ACTION,
    SCENE_STATUS_CHANGE_ACTION,
    SHOT_DRAFT_CONFIRM_ACTION,
    SHOT_STATUS_CHANGE_ACTION,
)

_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_TENANT = uuid4()


def _scene(**overrides: object) -> Scene:
    base = {
        "entity_id": "scene-1",
        "project_id": "proj-1",
        "episode_id": "ep-1",
        "scene_code": "S01",
        "created_by": "user-1",
        "tenant_id": _TENANT,
    }
    base.update(overrides)
    return Scene(**base)  # type: ignore[arg-type]


def _shot(**overrides: object) -> Shot:
    base = {
        "entity_id": "shot-1",
        "project_id": "proj-1",
        "scene_id": "scene-1",
        "shot_code": "S01-01",
        "shot_revision_id": "rev-1",
        "created_by": "user-1",
        "tenant_id": _TENANT,
    }
    base.update(overrides)
    return Shot(**base)  # type: ignore[arg-type]


def _draft(**overrides: object) -> ShotBreakdownDraft:
    base = {
        "entity_id": "draft-1",
        "project_id": "proj-1",
        "basis_script_revision_id": "script-1",
        "plan_name": "第一版",
        "generation_method": "AI",
        "scene_breakdowns": [{"scene_id": "scene-1", "shots": []}],
        "breakdown_scope": {"episode_ref": "E01"},
        "root_draft_ref": "draft-1",
        "created_by": "user-1",
        "tenant_id": _TENANT,
    }
    base.update(overrides)
    return ShotBreakdownDraft(**base)  # type: ignore[arg-type]


def _rework(**overrides: object) -> ReworkTask:
    base = {
        "entity_id": "task-1",
        "project_id": "proj-1",
        "issue_id": "issue-1",
        "target_ref": "shot-1",
        "defect_category": "LIGHTING",
        "severity": "MAJOR",
        "root_cause": "布光与分镜不一致",
        "owner": "artist-1",
        "return_node": "SHOT",
        "created_by": "qc-1",
        "tenant_id": _TENANT,
    }
    base.update(overrides)
    return ReworkTask(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
def test_scene_draft_can_activate_and_archive() -> None:
    scene = _scene()
    scene.transition_to(SceneStatus.ACTIVE, "audit-1", SCENE_STATUS_CHANGE_ACTION)
    assert scene.status is SceneStatus.ACTIVE
    assert scene.entity_version == 2

    scene.transition_to(SceneStatus.ARCHIVED, "audit-2", SCENE_STATUS_CHANGE_ACTION)
    assert scene.status is SceneStatus.ARCHIVED


def test_scene_archived_is_terminal() -> None:
    scene = _scene(status=SceneStatus.ARCHIVED)
    with pytest.raises(DomainError):
        scene.transition_to(SceneStatus.ACTIVE, "audit-1", SCENE_STATUS_CHANGE_ACTION)


def test_scene_rejects_action_that_does_not_own_the_edge() -> None:
    scene = _scene()
    with pytest.raises(DomainError):
        scene.transition_to(SceneStatus.ACTIVE, "audit-1", "SHOT.STATUS.CHANGE")


def test_scene_id_must_equal_entity_id() -> None:
    with pytest.raises(DomainError):
        _scene(scene_id="other-id")


# --------------------------------------------------------------------------- #
# Shot：6 条守卫边
# --------------------------------------------------------------------------- #
def test_shot_requires_blocked_reason_when_entering_blocked() -> None:
    shot = _shot()
    with pytest.raises(DomainError):
        shot.transition_to(ShotStatus.BLOCKED, "audit-1", SHOT_STATUS_CHANGE_ACTION)
    assert shot.status is ShotStatus.NOT_STARTED


def test_shot_enters_blocked_with_reason() -> None:
    shot = _shot()
    shot.blocked_reason = "灯光设备未到"
    shot.transition_to(ShotStatus.BLOCKED, "audit-1", SHOT_STATUS_CHANGE_ACTION)
    assert shot.status is ShotStatus.BLOCKED


def test_shot_cancel_requires_authorization_and_timestamp() -> None:
    shot = _shot()
    with pytest.raises(DomainError):
        shot.transition_to(ShotStatus.CANCELLED, "audit-1", SHOT_STATUS_CHANGE_ACTION)

    shot.cancellation_ref = "approval://cancel/1"
    with pytest.raises(DomainError):  # 缺 cancelled_at 仍然拒绝
        shot.transition_to(ShotStatus.CANCELLED, "audit-1", SHOT_STATUS_CHANGE_ACTION)

    shot.cancelled_at = _NOW
    shot.transition_to(ShotStatus.CANCELLED, "audit-1", SHOT_STATUS_CHANGE_ACTION)
    assert shot.status is ShotStatus.CANCELLED


def test_shot_cancelled_is_terminal() -> None:
    shot = _shot(
        status=ShotStatus.CANCELLED,
        cancellation_ref="approval://cancel/1",
        cancelled_at=_NOW,
    )
    with pytest.raises(DomainError):
        shot.transition_to(ShotStatus.IN_PRODUCTION, "audit-1", SHOT_STATUS_CHANGE_ACTION)


def test_shot_rework_rollback_requires_basis() -> None:
    shot = _shot(status=ShotStatus.PRODUCTION_COMPLETE)
    with pytest.raises(DomainError):
        shot.transition_to(ShotStatus.IN_PRODUCTION, "audit-1", SHOT_STATUS_CHANGE_ACTION)

    shot.rework_basis_ref = "rework-1"
    shot.transition_to(ShotStatus.IN_PRODUCTION, "audit-1", SHOT_STATUS_CHANGE_ACTION)
    assert shot.status is ShotStatus.IN_PRODUCTION
    # 返工依据在回到生产中后保留（契约 note：保留 rework_basis_ref）。
    assert shot.rework_basis_ref == "rework-1"


def test_shot_rejects_unregistered_backward_edge() -> None:
    shot = _shot(status=ShotStatus.PRODUCTION_COMPLETE)
    with pytest.raises(DomainError):
        # PRODUCTION_COMPLETE → BLOCKED 未登记
        shot.transition_to(ShotStatus.BLOCKED, "audit-1", SHOT_STATUS_CHANGE_ACTION)


def test_shot_evidence_must_be_empty_in_normal_states() -> None:
    with pytest.raises(DomainError):
        _shot(blocked_reason="不该带原因")


# --------------------------------------------------------------------------- #
# ShotBreakdownDraft
# --------------------------------------------------------------------------- #
def test_draft_claim_then_confirm() -> None:
    draft = _draft()
    draft.transition_to(ShotDraftStatus.IN_REVIEW, "audit-1", SHOT_DRAFT_CONFIRM_ACTION)

    draft.confirmed_by = "director-1"
    draft.confirmed_at = _NOW
    draft.confirmed_shot_revision_refs = ["rev-1"]
    draft.transition_to(ShotDraftStatus.CONFIRMED, "audit-2", SHOT_DRAFT_CONFIRM_ACTION)
    assert draft.status is ShotDraftStatus.CONFIRMED


def test_draft_confirm_requires_outputs() -> None:
    draft = _draft(status=ShotDraftStatus.IN_REVIEW)
    draft.confirmed_by = "director-1"
    draft.confirmed_at = _NOW
    with pytest.raises(DomainError):  # 没有 confirmed_shot_revision_refs
        draft.transition_to(ShotDraftStatus.CONFIRMED, "audit-1", SHOT_DRAFT_CONFIRM_ACTION)


def test_draft_confirmed_is_terminal() -> None:
    draft = _draft(
        status=ShotDraftStatus.CONFIRMED,
        confirmed_by="director-1",
        confirmed_at=_NOW,
        confirmed_shot_revision_refs=["rev-1"],
    )
    with pytest.raises(DomainError):
        draft.transition_to(ShotDraftStatus.IN_REVIEW, "audit-1", SHOT_DRAFT_CONFIRM_ACTION)


def test_draft_return_requires_reason() -> None:
    draft = _draft(status=ShotDraftStatus.IN_REVIEW)
    draft.returned_by = "qc-1"
    draft.returned_at = _NOW
    with pytest.raises(DomainError):
        draft.transition_to(ShotDraftStatus.RETURNED, "audit-1", "SHOT.DRAFT.RETURN")


# --------------------------------------------------------------------------- #
# ReworkTask
# --------------------------------------------------------------------------- #
def test_rework_rejected_has_no_entry_edge() -> None:
    task = _rework()
    with pytest.raises(DomainError):
        task.transition_to(
            ReworkTaskStatus.REJECTED, "audit-1", REWORK_TASK_STATUS_CHANGE_ACTION
        )


def test_rework_happy_path() -> None:
    task = _rework()
    for target in (
        ReworkTaskStatus.IN_PROGRESS,
        ReworkTaskStatus.DONE,
        ReworkTaskStatus.VERIFYING,
    ):
        task.transition_to(target, f"audit-{target.value}", REWORK_TASK_STATUS_CHANGE_ACTION)
    assert task.status is ReworkTaskStatus.VERIFYING


def test_rework_reviewer_cannot_be_owner() -> None:
    task = _rework(status=ReworkTaskStatus.VERIFYING)
    with pytest.raises(DomainError):
        task.assert_reviewer_not_owner("artist-1")
    task.assert_reviewer_not_owner("qc-2")
