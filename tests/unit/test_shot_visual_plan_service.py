"""ShotVisualPlanService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖（19.2 / C.7 / B2 / B4）：
- Agent 可创建 DRAFT，但不得推进生命周期（审阅/锁定拒绝）；
- 职责分离：审阅人不得与创建人同一主体（C.5.1）；
- 逐字段授权锁定（锁定人/时间/理由/版本齐备）；
- 终态不可原地复活。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal, SubjectType
from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan
from services.shot_planning.repository.shot_visual_plan_repo import (
    ShotVisualPlanRepository,
)
from services.shot_planning.service.visual_plan_service import ShotVisualPlanService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(
    subject_id: str = "director_001",
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )


class _FakePlanRepository(ShotVisualPlanRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotVisualPlan] = {}

    async def add(self, plan: ShotVisualPlan) -> None:
        self._store[plan.entity_id] = plan

    async def save(self, plan: ShotVisualPlan, expected_version: int) -> None:
        # 聚合根原地变更：save 收到的 expected_version 是读取时版本，
        # 变更后应恰好推进 1（乐观锁口径由真实仓储的条件 UPDATE 保证）。
        assert plan.entity_version == expected_version + 1, (
            "版本推进必须恰好 +1（乐观锁口径）"
        )
        self._store[plan.entity_id] = plan

    async def get_by_id(self, entity_id: str) -> ShotVisualPlan | None:
        return self._store.get(entity_id)


def _make_service() -> tuple[ShotVisualPlanService, _FakePlanRepository]:
    repo = _FakePlanRepository()
    return ShotVisualPlanService(repo), repo


async def _approved_plan(
    service: ShotVisualPlanService, *, creator: str = "agent_001"
) -> ShotVisualPlan:
    """构造一个 APPROVED 态方案（Agent 创建，导演审阅通过）。"""
    plan = await service.create_plan(
        shot_id="shot_001",
        project_id="proj_001",
        intent_summary="角色 A 情绪转折",
        camera_language="手持跟随",
        shot_size="MEDIUM",
        lens_range="35-50mm",
        motion_intent="跟随角色移动",
        lighting_intent="低照度暖光",
        open_fields=["composition_notes", "color_mood"],
        principal=_principal(creator, SubjectType.AGENT),
    )
    await service.submit_plan(plan_id=plan.entity_id, principal=_principal("td_001"))
    await service.approve_plan(plan_id=plan.entity_id, principal=_principal())
    return plan


async def test_agent_creates_draft_and_cannot_progress() -> None:
    """Agent 只能产出 DRAFT（B2）：送审/审阅/锁定全部拒绝。"""
    service, _ = _make_service()
    plan = await service.create_plan(
        shot_id="shot_001",
        project_id="proj_001",
        intent_summary="意图",
        camera_language="固定机位",
        shot_size="WIDE",
        lens_range="24-35mm",
        motion_intent="静态",
        lighting_intent="自然光",
        open_fields=["composition_notes", "color_mood"],
        principal=_principal("agent_001", SubjectType.AGENT),
    )
    assert plan.status.value == "DRAFT"

    for method in (
        "submit_plan",
        "approve_plan",
        "reject_plan",
        "lock_plan_fields",
    ):
        with pytest.raises(DomainError) as exc_info:
            await getattr(service, method)(
                plan_id=plan.entity_id,
                principal=_principal("agent_001", SubjectType.AGENT),
                **(
                    {"field_names": ["color_mood"], "reason": "理由"}
                    if method == "lock_plan_fields"
                    else {}
                ),
            )
        assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"


async def test_separation_of_duties() -> None:
    """审阅人不得与创建人同一主体（C.5.1 职责分离）。"""
    service, _ = _make_service()
    plan = await service.create_plan(
        shot_id="shot_001",
        project_id="proj_001",
        intent_summary="意图",
        camera_language="固定机位",
        shot_size="WIDE",
        lens_range="24-35mm",
        motion_intent="静态",
        lighting_intent="自然光",
        open_fields=["composition_notes", "color_mood"],
        principal=_principal("director_001"),  # 人类创建
    )
    await service.submit_plan(plan_id=plan.entity_id, principal=_principal("td_001"))
    with pytest.raises(DomainError) as exc_info:
        await service.approve_plan(
            plan_id=plan.entity_id, principal=_principal("director_001")
        )
    assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"


async def test_reject_is_terminal() -> None:
    """REVIEW → REJECTED 是终态：不可复活，变更须新建 revision。"""
    service, _ = _make_service()
    plan = await service.create_plan(
        shot_id="shot_001",
        project_id="proj_001",
        intent_summary="意图",
        camera_language="固定机位",
        shot_size="WIDE",
        lens_range="24-35mm",
        motion_intent="静态",
        lighting_intent="自然光",
        open_fields=["composition_notes", "color_mood"],
        principal=_principal("agent_001", SubjectType.AGENT),
    )
    await service.submit_plan(plan_id=plan.entity_id, principal=_principal("td_001"))
    rejected = await service.reject_plan(
        plan_id=plan.entity_id, principal=_principal()
    )
    assert rejected.status.value == "REJECTED"

    with pytest.raises(DomainError) as exc_info:
        await service.submit_plan(plan_id=plan.entity_id, principal=_principal("td_001"))
    assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


async def test_lock_fields_records_metadata() -> None:
    """逐字段授权锁定：锁定人/时间/理由/版本齐备（19.2）。"""
    service, _ = _make_service()
    plan = await _approved_plan(service)
    locked = await service.lock_plan_fields(
        plan_id=plan.entity_id,
        field_names=["composition_notes"],
        reason="构图定稿",
        principal=_principal(),
    )
    assert locked.status.value == "PARTIALLY_LOCKED"
    entry = locked.locked_fields[0]
    assert entry["field"] == "composition_notes"
    assert entry["locked_by"] == "director_001"
    assert entry["reason"] == "构图定稿"
    assert entry["locked_at"]
    assert entry["version"] >= 1
    assert locked.open_fields == ["color_mood"]

    fully = await service.lock_plan_fields(
        plan_id=plan.entity_id,
        field_names=["color_mood"],
        reason="色调定稿",
        principal=_principal(),
    )
    assert fully.status.value == "LOCKED"
    assert fully.open_fields == []
