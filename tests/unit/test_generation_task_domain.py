"""GenerationTask 聚合根与状态机单测（纯领域层，无 IO）。"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.state_machine import (
    assert_transition_generation_task,
    can_transition_generation_task,
)


def _task(**overrides: object) -> GenerationTask:
    """构造一个合法的 PENDING 任务（测试基线对象）。"""
    defaults: dict[str, object] = {
        "entity_id": f"gen_{uuid.uuid4().hex[:12]}",
        "project_id": "proj_001",
        "shot_id": "SHOT_001",
        "route_id": "route_001",
        "shot_spec_ref": "spec_001",
        "visual_plan_ref": "vplan_001",
        "constraint_snapshot_ref": "NOT_APPLICABLE",
        "budget_reservation_ref": "budget_res_001",
        "quota_reservation_ref": "quota_res_001",
        "dependency_refs": [],
        "attempt": 1,
        "created_by": "producer_001",
        "tenant_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return GenerationTask(**defaults)  # type: ignore[arg-type]


class TestGenerationTaskAggregate:
    def test_construct_pending_minimal(self) -> None:
        task = _task()
        assert task.status is GenerationTaskStatus.PENDING
        assert task.owner_module == "generation-execution"
        assert task.ENTITY_TYPE == "GenerationTask"

    def test_required_refs_reject_empty(self) -> None:
        """准入引用字段非空（ACT-GEN-NEG-001 的结构性准入面）。"""
        for field in (
            "project_id",
            "shot_id",
            "route_id",
            "shot_spec_ref",
            "visual_plan_ref",
            "constraint_snapshot_ref",
            "budget_reservation_ref",
            "quota_reservation_ref",
        ):
            with pytest.raises(DomainError) as exc_info:
                _task(**{field: ""})
            assert exc_info.value.error_code.code == "VALIDATION_ERROR"
            assert field in str(exc_info.value)

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(DomainError):
            _task(attempt=0)

    def test_retry_requires_retry_of(self) -> None:
        """attempt > 1 必须携带 retry_of（重试以新任务承载，9.4）。"""
        with pytest.raises(DomainError) as exc_info:
            _task(attempt=2)
        assert "retry_of" in str(exc_info.value)

    def test_first_task_may_not_carry_retry_of(self) -> None:
        """首建任务（attempt=1）不得携带 retry_of——重试链不可伪造。"""
        with pytest.raises(DomainError):
            _task(attempt=1, retry_of="gen_failed_001")

    def test_retry_task_chains_attempt(self) -> None:
        task = _task(attempt=2, retry_of="gen_failed_001")
        assert task.attempt == 2
        assert task.retry_of == "gen_failed_001"

    def test_to_dict_maps_generation_task_id(self) -> None:
        task = _task()
        data = task.to_dict()
        assert data["generation_task_id"] == task.entity_id
        assert data["entity_type"] == "GenerationTask"
        assert data["status"] == "PENDING"
        assert data["attempt"] == 1
        # 契约 required 字段对齐（A6 冻结定义）。
        for key in (
            "shot_spec_ref",
            "visual_plan_ref",
            "constraint_snapshot_ref",
            "budget_reservation_ref",
            "quota_reservation_ref",
        ):
            assert key in data


class TestGenerationTaskStateMachine:
    def test_registered_edges(self) -> None:
        assert can_transition_generation_task(
            GenerationTaskStatus.PENDING, GenerationTaskStatus.RUNNING
        )
        assert can_transition_generation_task(
            GenerationTaskStatus.RUNNING, GenerationTaskStatus.SUCCEEDED
        )
        assert can_transition_generation_task(
            GenerationTaskStatus.RUNNING, GenerationTaskStatus.FAILED
        )

    def test_terminal_states_have_no_out_edges(self) -> None:
        """终态不可原地复活（红线）：FAILED/SUCCEEDED 无出边。"""
        for terminal in (GenerationTaskStatus.SUCCEEDED, GenerationTaskStatus.FAILED):
            for target in GenerationTaskStatus:
                assert not can_transition_generation_task(terminal, target)

    def test_assert_transition_denied_raises(self) -> None:
        with pytest.raises(DomainError) as exc_info:
            assert_transition_generation_task(
                GenerationTaskStatus.FAILED, GenerationTaskStatus.RUNNING
            )
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"
