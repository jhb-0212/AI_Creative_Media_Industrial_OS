"""ReworkTaskService：执行人身份可信性（P1-02）与职责分离。

核心命题（防止审计报告指出的「可信 Principal vs 自报字符串」漏洞回归）：
- owner 由可信 created_by 派生，服务签名根本不接 owner 入参——调用方无法自报；
- 关闭时职责分离走 identity 单一权威判定，复核人缺失或与执行人相同都拒绝；
- 拒绝时无落库、无审计事实（fail-closed 不带副作用）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import ProjectTier, ReworkTaskStatus
from services.project_planning.domain.project import Project
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ReworkTaskRepository,
)
from services.project_planning.service.rework_task_service import ReworkTaskService

_TENANT = uuid4()
_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class _Tasks(ReworkTaskRepository):
    def __init__(self) -> None:
        self.rows: dict[str, ReworkTask] = {}
        self.added: list[ReworkTask] = []
        self.saved: list[tuple[str, int]] = []

    async def add(self, task: ReworkTask) -> None:
        self.rows[task.entity_id] = task
        self.added.append(task)

    async def save(self, task: ReworkTask, expected_version: int) -> None:
        self.saved.append((task.entity_id, expected_version))
        self.rows[task.entity_id] = task

    async def get_by_id(self, entity_id: str) -> ReworkTask | None:
        return self.rows.get(entity_id)


class _Projects(ProjectRepository):
    """只需 get_by_id 判存在；其余抽象方法给出不可用实现（aints 不该被调用）。"""

    def __init__(self, project_id: str) -> None:
        self.project = Project(
            entity_id=project_id,
            project_name="测试项目",
            project_tier=ProjectTier.B,
            created_by="creator",
            tenant_id=_TENANT,
        )

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self.project if entity_id == self.project.entity_id else None

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        raise AssertionError("本用例不该走带锁读")

    async def add(self, project: Project) -> None:
        raise AssertionError("本用例不该新增项目")

    async def save(self, project: Project, expected_version: int) -> None:
        raise AssertionError("本用例不该保存项目")

    async def get_by_project_code(self, project_code: str) -> Project | None:
        raise AssertionError("本用例不该按编号查")

    async def list(self, **kwargs: Any) -> list[Project]:
        raise AssertionError("本用例不该列举项目")


@dataclass
class _Outbox(OutboxWriter):
    events: list[tuple[str, str]] = field(default_factory=list)
    transitions: list[Any] = field(default_factory=list)

    def record(self, **kwargs: Any) -> UUID:
        self.events.append((kwargs["aggregate_type"], kwargs["event_type"]))
        return uuid4()

    def record_transition(self, *, fact: Any, event: Any) -> UUID:
        self.transitions.append(fact)
        return uuid4()


def _service(project_id: str = "proj-1") -> tuple[ReworkTaskService, _Tasks, _Outbox]:
    tasks = _Tasks()
    outbox = _Outbox()
    service = ReworkTaskService(tasks, _Projects(project_id), outbox)  # type: ignore[arg-type]
    return service, tasks, outbox


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_id": "proj-1",
        "issue_id": "issue-1",
        "target_ref": "asset-1",
        "defect_category": "画面",
        "severity": "HIGH",
        "root_cause": "灯光不足",
        "return_node": "LIGHTING",
        "affected_asset_version_ids": [],
        "downstream_task_ids": [],
        "created_by": "qc-user",  # 可信 Principal
        "tenant_id": _TENANT,
    }
    base.update(overrides)
    return base


def test_create_task_signature_has_no_owner_parameter() -> None:
    """服务签名层面就不接 owner——调用方无法自报执行人（编译期契约）。"""
    params = inspect.signature(ReworkTaskService.create_task).parameters
    assert "owner" not in params, "create_task 不得接受 owner，必须从可信 created_by 派生"


@pytest.mark.asyncio
async def test_owner_is_derived_from_trusted_created_by() -> None:
    """执行人恒等于可信 Principal：创建者认领，落库与事件一致。"""
    service, tasks, outbox = _service()
    task = await service.create_task(**_create_kwargs())

    assert task.owner == "qc-user", "owner 必须来自 created_by，不是请求体"
    assert tasks.added[0].owner == "qc-user"
    created_event = next(e for e in outbox.events if e[1] == "REWORK_TASK_CREATED")
    assert created_event[0] == "ReworkTask"


async def _advance_to_verifying(
    service: ReworkTaskService, task: ReworkTask, actor: str = "artist-user"
) -> None:
    """按登记边逐级推进 OPEN→IN_PROGRESS→DONE→VERIFYING（不跳步）。"""
    for status in (
        ReworkTaskStatus.IN_PROGRESS,
        ReworkTaskStatus.DONE,
        ReworkTaskStatus.VERIFYING,
    ):
        await service.change_status(
            entity_id=task.entity_id, target_status=status, changed_by=actor, tenant_id=_TENANT
        )


@pytest.mark.asyncio
async def test_closing_with_owner_as_reviewer_is_denied() -> None:
    """复核人=执行人时拒绝（可信 vs 可信比较，职责分离红线）。"""
    service, tasks, outbox = _service()
    task = await service.create_task(**_create_kwargs())
    await _advance_to_verifying(service, task)

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=task.entity_id,
            target_status=ReworkTaskStatus.CLOSED,
            changed_by="qc-user",  # 与 owner 同一主体 → 自批
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "职责分离" in exc.value.detail or "同一主体" in exc.value.detail
    assert tasks.rows[task.entity_id].status is ReworkTaskStatus.VERIFYING
    assert outbox.transitions[-1].to_status == ReworkTaskStatus.VERIFYING.value


@pytest.mark.asyncio
async def test_closing_by_another_subject_succeeds() -> None:
    """另一可信主体复核可关闭——证明拒绝不是「一律不放行」。"""
    service, _, _ = _service()
    task = await service.create_task(**_create_kwargs())
    await _advance_to_verifying(service, task)
    task = await service.change_status(
        entity_id=task.entity_id,
        target_status=ReworkTaskStatus.CLOSED,
        changed_by="reviewer-user",  # 与 owner(qc-user) 不同的独立主体
        tenant_id=_TENANT,
    )
    assert task.status is ReworkTaskStatus.CLOSED
