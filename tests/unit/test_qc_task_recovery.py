"""人工恢复入队的判定闸门（C7-b）。

被测命题：`POST /qc-tasks` 的判据**只能来自数据库事实**，不能来自请求体；
并且它与自动入队共用同一条幂等事实。

为什么这些判据要单测锁住、不能只靠集成测试
------------------------------------------
集成测试证明「这条请求现在被拒了」，但证明不了「拒绝的理由是不是因为请求体
被当成了事实」。这六条判据里有三条（终态、产物引用、口径版本）只要有人
「顺手」把参数从 facts 换成 body，行为在**正常路径上完全不变**——只有
「任务还没成功」或「没有产物引用」时才会分叉，而那两个场景在正向用例里
永远不会出现。所以这里对每条拒绝各自造一次事实。

反面清单同样重要：这些用例断言「拒绝时没有写入、没有事件」。
若只断言异常类型，一种常见的坏改动（先 add 再校验）会同时通过。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.generation_ports import GenerationFacts
from packages.shared.in_memory import InMemoryOutboxWriter
from packages.shared.port_errors import DuplicateEntityError
from services.qc_governance.domain.qc_task import EnqueueOrigin, QCTask
from services.qc_governance.service.qc_task_service import QCTaskService

_TENANT = UUID("00000000-0000-0000-0000-000000000055")


class _FakeRepo:
    """最小仓储替身：只实现服务用到的两个方法。

    ``conflict_on_add`` 模拟「先查都通过、唯一键只有一个能成立」的并发窗口，
    用来证明 23505 被收敛成可判定的幂等冲突，而不是漏成 500。
    """

    def __init__(self, *, conflict_on_add: bool = False) -> None:
        self.rows: list[QCTask] = []
        self._conflict_on_add = conflict_on_add

    async def find_by_generation_task(self, generation_task_id: str) -> QCTask | None:
        for task in self.rows:
            if task.generation_task_id == generation_task_id:
                return task
        return None

    async def add(self, task: QCTask) -> QCTask:
        if self._conflict_on_add:
            raise DuplicateEntityError(
                task.entity_id,
                constraint="uq_qc_governance_qc_tasks_generation_task",
            )
        self.rows.append(task)
        return task


def _facts(
    *,
    generation_task_id: str = "gen-db",
    tenant_id: UUID = _TENANT,
    status: str = "SUCCEEDED",
    project_id: str = "proj-db",
    shot_id: str = "shot-db",
    asset_version_id: str = "asset-db",
    quality_profile_version: str = "qp-db",
) -> GenerationFacts:
    return GenerationFacts(
        generation_task_id=generation_task_id,
        tenant_id=tenant_id,
        status=status,
        project_id=project_id,
        shot_id=shot_id,
        asset_version_id=asset_version_id,
        quality_profile_version=quality_profile_version,
    )


def _service(
    *, conflict_on_add: bool = False
) -> tuple[QCTaskService, _FakeRepo, InMemoryOutboxWriter]:
    repo = _FakeRepo(conflict_on_add=conflict_on_add)
    outbox = InMemoryOutboxWriter()
    return QCTaskService(repo, outbox), repo, outbox


async def test_recover_takes_every_fact_from_database() -> None:
    """恢复动作只说「哪一条」，产物引用与口径版本一律来自事实行。"""
    service, repo, outbox = _service()

    task = await service.recover(
        facts=_facts(), operator_id="ops-1", reason="relay 事件丢失，人工补录"
    )

    assert task.project_id == "proj-db"
    assert task.shot_id == "shot-db"
    assert task.asset_version_id == "asset-db"
    assert task.quality_profile_version == "qp-db"
    assert task.generation_task_id == "gen-db"
    # 操作者身份只从 Principal 来（此处即 operator_id），不来自任何请求字段。
    assert task.created_by == "ops-1"
    assert len(repo.rows) == 1

    payload = outbox.events[-1]["payload"]
    assert payload["source"] == "MANUAL"
    assert payload["operator_id"] == "ops-1"
    assert payload["reason"] == "relay 事件丢失，人工补录"


@pytest.mark.parametrize(
    "status",
    ["REQUESTED", "RUNNING", "AWAITING_ASSET_REGISTRATION", "FAILED"],
)
async def test_recover_rejects_non_terminal_success(status: str) -> None:
    """只有登记成功终态可入队：其余状态一律 409，且不留下任何行与事件。"""
    service, repo, outbox = _service()

    with pytest.raises(DomainError) as exc:
        await service.recover(facts=_facts(status=status), operator_id="ops-1", reason="补录")

    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert repo.rows == []
    assert outbox.events == []


async def test_recover_rejects_missing_asset_version() -> None:
    """终态成功却缺产物引用是数据不一致：不猜一个值补上，直接拒绝。"""
    service, repo, outbox = _service()

    with pytest.raises(DomainError) as exc:
        await service.recover(facts=_facts(asset_version_id=""), operator_id="ops-1", reason="补录")

    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert repo.rows == []
    assert outbox.events == []


async def test_recover_rejects_missing_quality_profile_version() -> None:
    """缺口径版本同样拒绝：否则会入队一条不知道该按什么标准检的待检项。"""
    service, repo, outbox = _service()

    with pytest.raises(DomainError) as exc:
        await service.recover(
            facts=_facts(quality_profile_version=""), operator_id="ops-1", reason="补录"
        )

    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert repo.rows == []
    assert outbox.events == []


async def test_recover_requires_reason() -> None:
    """人工绕过正常链路，「为什么」是唯一无法从数据推导出来的信息。"""
    service, repo, _outbox = _service()

    with pytest.raises(DomainError) as exc:
        await service.recover(facts=_facts(), operator_id="ops-1", reason="   ")

    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert repo.rows == []


async def test_recover_rejects_duplicate_via_precheck() -> None:
    """已在队列里的任务不再补录：409 而不是静默成功或 500。"""
    service, repo, _outbox = _service()
    await service.recover(facts=_facts(), operator_id="ops-1", reason="第一次")

    with pytest.raises(DomainError) as exc:
        await service.recover(facts=_facts(), operator_id="ops-2", reason="第二次")

    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
    assert len(repo.rows) == 1


async def test_recover_converges_unique_conflict_to_idempotency() -> None:
    """并发窗口：先查都通过，唯一键只有一个能成立——必须收敛，不能冒成 500。

    这条用例是 C7-5 的核心：唯一约束只有在**冲突被翻译**之后才等于幂等。
    此前仓储是裸 flush，23505 会以驱动异常的形式冒出去，relay 侧会把收益
    当成失败反复重试直到死信。
    """
    service, _repo, outbox = _service(conflict_on_add=True)

    with pytest.raises(DomainError) as exc:
        await service.recover(facts=_facts(), operator_id="ops-1", reason="补录")

    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
    # 冲突即未创建：不能留下一条「QUEUED 已发出」的假事件。
    assert outbox.events == []


def test_relay_origin_needs_no_reason_but_manual_does() -> None:
    """自动入队不需要解释自己为什么存在；人工恢复必须解释。"""
    relay = EnqueueOrigin(source="RELAY", operator_id="outbox-relay")
    assert relay.reason == ""

    with pytest.raises(DomainError) as exc:
        EnqueueOrigin(source="MANUAL", operator_id="ops-1")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR


def test_origin_requires_operator() -> None:
    """没有操作者的入队事件无法审计，构造期就失败。"""
    with pytest.raises(DomainError) as exc:
        EnqueueOrigin(source="RELAY", operator_id=" ")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR


async def test_recover_uses_fact_tenant_as_single_source() -> None:
    """事实行的租户直接决定任务的租户，服务层不接受第二份 tenant 参数。

    这条守的是「一份事实一个来源」：若再传一个 tenant_id，两者不一致时会写出
    「任务属于 A 租户、事实来自 B 租户」的行，而这在 RLS 之外没有地方会拦。
    """
    facts = _facts(tenant_id=uuid4())
    service, repo, _outbox = _service()

    task = await service.recover(facts=facts, operator_id="ops-1", reason="补录")

    assert task.tenant_id == facts.tenant_id
    assert repo.rows[0].tenant_id == facts.tenant_id
