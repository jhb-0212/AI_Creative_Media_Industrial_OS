"""生成任务租约语义单测（纯领域层，无 IO）。

要证明的四件事（缺一条，"崩溃恢复"就只是说法）：
1. 领取即登记租约：到期时刻 = now + lease，且 started_at 被记录；
2. 接管是**状态不变**的租约写入，且不覆盖 started_at（首次开始是事实）；
3. 租约是栅栏：非持有者续租/写终态一律被拒——被接管的僵尸不能覆盖新执行；
4. 非法参数（非正租约）在领域层就拒绝，不靠数据库兜底。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.errors.error_codes import DomainError
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.repository.generation_task_repo import (
    GenerationTaskRepository,
)
from services.generation_execution.service.generation_service import (
    GenerationTaskLifecycle,
)

_NOW = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)


def _task(**overrides: object) -> GenerationTask:
    defaults: dict[str, object] = {
        "entity_id": f"gen_{uuid.uuid4().hex[:12]}",
        "project_id": "proj_lease_001",
        "shot_id": "SHOT_001",
        "route_id": "route_001",
        "shot_spec_ref": "spec_001",
        "visual_plan_ref": "vplan_001",
        "constraint_snapshot_ref": "NOT_APPLICABLE",
        "budget_reservation_ref": "budget_res_001",
        "quota_reservation_ref": "quota_res_001",
        "dependency_refs": [],
        "attempt": 1,
        "created_by": "director_001",
        "tenant_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return GenerationTask(**defaults)  # type: ignore[arg-type]


class _FakeTaskRepository(GenerationTaskRepository):
    """最小假仓储：只记录写入，不模拟并发（并发由集成测试证明）。"""

    def __init__(self, *, take_over_result: bool = True) -> None:
        self.saved: list[tuple[str, int]] = []
        self.take_over_calls: list[dict[str, object]] = []
        self._take_over_result = take_over_result

    async def add(self, task: GenerationTask) -> None:
        raise AssertionError("租约用例不涉及受理登记")

    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        return None

    async def save(
        self, task: GenerationTask, *, expected_version: int
    ) -> GenerationTask:
        self.saved.append((task.entity_id, expected_version))
        return task

    async def claim_pending(
        self,
        *,
        actor: str,
        worker_id: str,
        lease_expires_at: datetime,
        now: datetime,
        limit: int,
    ) -> list[str]:
        return []

    async def list_expired_leases(
        self, limit: int, *, now: datetime
    ) -> list[GenerationTask]:
        return []

    async def take_over(
        self,
        task_id: str,
        *,
        worker_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        self.take_over_calls.append(
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "expires_at": expires_at,
                "now": now,
            }
        )
        return self._take_over_result


class TestClaim:
    async def test_claim_registers_lease_and_start_time(self) -> None:
        repo = _FakeTaskRepository()
        task = _task()
        await GenerationTaskLifecycle(repo).claim(
            task,
            actor="svc_generation_executor",
            lease_owner="host-a:100",
            lease_seconds=300,
            now=_NOW,
        )
        assert task.status is GenerationTaskStatus.RUNNING
        assert task.lease_owner == "host-a:100"
        assert task.lease_expires_at == _NOW + timedelta(seconds=300)
        assert task.started_at == _NOW
        # 乐观锁：以读取时的版本作为 expected（不是自增后的）。
        assert repo.saved == [(task.entity_id, task.entity_version - 1)]

    async def test_claim_rejects_non_positive_lease(self) -> None:
        """租约窗口为 0 不是"立刻回收"，是配置错误——领域层直接拒绝。"""
        for seconds in (0, -1):
            with pytest.raises(DomainError) as exc_info:
                await GenerationTaskLifecycle(_FakeTaskRepository()).claim(
                    _task(),
                    actor="svc_generation_executor",
                    lease_owner="host-a:100",
                    lease_seconds=seconds,
                    now=_NOW,
                )
            assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    async def test_claim_rejects_terminal_task(self) -> None:
        task = _task(status=GenerationTaskStatus.FAILED)
        with pytest.raises(DomainError) as exc_info:
            await GenerationTaskLifecycle(_FakeTaskRepository()).claim(
                task,
                actor="svc_generation_executor",
                lease_owner="host-a:100",
                lease_seconds=300,
                now=_NOW,
            )
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


class TestTakeOver:
    async def test_take_over_keeps_status_and_first_start(self) -> None:
        repo = _FakeTaskRepository()
        started = _NOW - timedelta(hours=1)
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-dead:1",
            lease_expires_at=_NOW - timedelta(minutes=1),
            started_at=started,
        )
        await GenerationTaskLifecycle(repo).take_over(
            task,
            actor="svc_generation_executor",
            lease_owner="host-b:2",
            lease_seconds=300,
            now=_NOW,
        )
        # 状态不变：接管不是状态转移（租约与状态正交）。
        assert task.status is GenerationTaskStatus.RUNNING
        # 快照不被改写：实际归属以数据库为准，调用方必须重读。
        assert task.lease_owner == "host-dead:1"
        assert task.lease_expires_at == _NOW - timedelta(minutes=1)
        # 首次开始执行是事实：接管不得把它改写成本次尝试的时间。
        assert task.started_at == started
        call = repo.take_over_calls[0]
        assert call["task_id"] == task.entity_id
        assert call["worker_id"] == "host-b:2"
        assert call["expires_at"] == _NOW + timedelta(seconds=300)

    async def test_take_over_cas_miss_is_concurrent_modification(self) -> None:
        """CAS 未命中（租约未到期/已被抢）：幂等跳过，不是业务失败。"""
        repo = _FakeTaskRepository(take_over_result=False)
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-a:1",
            lease_expires_at=_NOW + timedelta(minutes=5),
        )
        with pytest.raises(DomainError) as exc_info:
            await GenerationTaskLifecycle(repo).take_over(
                task,
                actor="svc_generation_executor",
                lease_owner="host-b:2",
                lease_seconds=300,
                now=_NOW,
            )
        assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"

    async def test_take_over_rejects_non_running_task(self) -> None:
        task = _task(status=GenerationTaskStatus.SUCCEEDED)
        with pytest.raises(DomainError) as exc_info:
            await GenerationTaskLifecycle(_FakeTaskRepository()).take_over(
                task,
                actor="svc_generation_executor",
                lease_owner="host-b:2",
                lease_seconds=300,
                now=_NOW,
            )
        assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"


class TestFencing:
    async def test_renew_by_non_holder_rejected(self) -> None:
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-a:1",
            lease_expires_at=_NOW + timedelta(minutes=1),
        )
        with pytest.raises(DomainError) as exc_info:
            await GenerationTaskLifecycle(_FakeTaskRepository()).renew(
                task,
                lease_owner="host-b:2",
                lease_seconds=300,
                now=_NOW,
            )
        assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"

    async def test_renew_by_holder_extends_lease(self) -> None:
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-a:1",
            lease_expires_at=_NOW + timedelta(minutes=1),
        )
        await GenerationTaskLifecycle(_FakeTaskRepository()).renew(
            task,
            lease_owner="host-a:1",
            lease_seconds=300,
            now=_NOW,
        )
        assert task.lease_expires_at == _NOW + timedelta(seconds=300)

    async def test_zombie_cannot_write_terminal_state(self) -> None:
        """租约被接管后，旧持有者的成功/失败写入都必须被拒（栅栏）。"""
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-b:2",  # 已被他人接管
            lease_expires_at=_NOW + timedelta(minutes=5),
        )
        lifecycle = GenerationTaskLifecycle(_FakeTaskRepository())
        with pytest.raises(DomainError) as succeed_exc:
            await lifecycle.succeed(
                task,
                result_ref="av_x",
                actor="svc_generation_executor",
                lease_owner="host-a:1",
                now=_NOW,
            )
        assert succeed_exc.value.error_code.code == "CONCURRENT_MODIFICATION"
        with pytest.raises(DomainError) as fail_exc:
            await lifecycle.fail(
                task,
                reason="adapter:TimeoutError: slow",
                actor="svc_generation_executor",
                lease_owner="host-a:1",
                now=_NOW,
            )
        assert fail_exc.value.error_code.code == "CONCURRENT_MODIFICATION"
        # 状态没有被写动：拒绝发生在写入之前。
        assert task.status is GenerationTaskStatus.RUNNING
        assert task.result_ref is None

    async def test_holder_succeeds_with_completion_time(self) -> None:
        repo = _FakeTaskRepository()
        task = _task(
            status=GenerationTaskStatus.RUNNING,
            lease_owner="host-a:1",
            lease_expires_at=_NOW + timedelta(minutes=5),
        )
        await GenerationTaskLifecycle(repo).succeed(
            task,
            result_ref="av_gen_001",
            actor="svc_generation_executor",
            lease_owner="host-a:1",
            now=_NOW,
        )
        assert task.status is GenerationTaskStatus.SUCCEEDED
        assert task.result_ref == "av_gen_001"
        assert task.completed_at == _NOW
        assert repo.saved == [(task.entity_id, task.entity_version - 1)]
