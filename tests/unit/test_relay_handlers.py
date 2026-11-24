"""Outbox 生成事件处理器的判据（P1-03）。

判据围绕三个语义，而不是实现细节：

1. **载荷自足**：缺任何一项必需事实的事件必须显式失败（→ 积压/死信可见），
   不能被当成功 ack 掉。删字段的历史事件若被静默吞掉，产物会永久失检。
2. **幂等**：重复投递不产生第二条待检项，也不报错。outbox 是至少一次投递，
   重复是正常语义而非缺陷。
3. **顺序**：入队由事件触发，事件与生成终态同事务——待检项不可能先于生成成功。
   第 3 条由「Worker 不再直接入队」的回归测试保证（见 test_generation_worker）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.in_memory import InMemoryOutboxWriter
from services.qc_governance.domain.qc_task import QCTask
from services.qc_governance.service.qc_task_service import QCTaskService
from workers.relay.consumer import OutboxEvent
from workers.relay.handlers import (
    GenerationSucceeded,
    MalformedEventError,
    build_generation_handlers,
    build_handlers,
    enqueue_idempotently,
    parse_generation_succeeded,
)

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_TASK_ID = "gen-task-1"


class FakeQCTaskRepository:
    """最小内存仓储：只实现 enqueue 走到的两个方法。"""

    def __init__(self) -> None:
        self.rows: dict[str, QCTask] = {}

    async def add(self, task: QCTask) -> QCTask:
        self.rows[task.entity_id] = task
        return task

    async def get_by_id(self, entity_id: str) -> QCTask | None:
        return self.rows.get(entity_id)

    async def find_by_generation_task(self, generation_task_id: str) -> QCTask | None:
        for task in self.rows.values():
            if task.generation_task_id == generation_task_id:
                return task
        return None

    async def list_expired(self, now: datetime) -> list[QCTask]:
        return []


def _event(payload: dict[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid4(),
        aggregate_type="GenerationTask",
        aggregate_id=_TASK_ID,
        tenant_id=_TENANT,
        event_type="GENERATION_SUCCEEDED",
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


def _good_payload() -> dict[str, Any]:
    # 与生产 emit 形状一致：advance_status 还会补 from/to/tenant/transition 与版本。
    return {
        "generation_task_id": _TASK_ID,
        "asset_version_id": "av-1",
        "quality_profile_version": "qp-1",
        "project_id": "proj-1",
        "shot_id": "shot-1",
        "worker_id": "svc:asset-registration",
        "evidence_ref": "receipt-ok",
        "attempt_count": 1,
        "schema_version": "1",
        "from_status": "AWAITING_ASSET_REGISTRATION",
        "to_status": "SUCCEEDED",
        "tenant_id": str(_TENANT),
        "transition_id": "tr-1",
    }


def _service() -> tuple[QCTaskService, FakeQCTaskRepository]:
    repo = FakeQCTaskRepository()
    return QCTaskService(repo, InMemoryOutboxWriter()), repo


# ---- 载荷解析 ------------------------------------------------------------- #


def test_parse_accepts_complete_payload() -> None:
    request = parse_generation_succeeded(_event(_good_payload()))
    assert request.generation_task_id == _TASK_ID
    assert request.asset_version_id == "av-1"
    assert request.quality_profile_version == "qp-1"
    assert request.project_id == "proj-1"
    assert request.shot_id == "shot-1"
    assert request.tenant_id == _TENANT


@pytest.mark.parametrize(
    "missing",
    [
        "generation_task_id",
        "asset_version_id",
        "quality_profile_version",
        "project_id",
    ],
)
def test_parse_rejects_missing_required_fact(missing: str) -> None:
    """缺任一必需事实都要报错——静默 ack 会让产物永久失检。"""
    payload = _good_payload()
    payload.pop(missing)
    with pytest.raises(MalformedEventError, match=missing):
        parse_generation_succeeded(_event(payload))


def test_parse_rejects_blank_value() -> None:
    """空串与缺失等价：都表示「这个事实没给」。"""
    payload = _good_payload()
    payload["asset_version_id"] = "   "
    with pytest.raises(MalformedEventError, match="asset_version_id"):
        parse_generation_succeeded(_event(payload))


def test_malformed_is_not_a_domain_error() -> None:
    """载荷违约是发出方问题，不是业务拒绝。

    若继承 DomainError，消费方可能按业务冲突去「收敛」，把违约事件 ack 掉。
    """
    assert not issubclass(MalformedEventError, DomainError)
    assert issubclass(MalformedEventError, ValueError)


# ---- 幂等入队 ------------------------------------------------------------- #


async def test_enqueue_creates_task_once() -> None:
    service, repo = _service()
    request = parse_generation_succeeded(_event(_good_payload()))

    assert await enqueue_idempotently(service, request) is True
    assert len(repo.rows) == 1
    assert next(iter(repo.rows.values())).generation_task_id == _TASK_ID


async def test_enqueue_twice_is_idempotent_success() -> None:
    """重复投递不报错、不新建——outbox 的至少一次语义要求如此。"""
    service, repo = _service()
    request = parse_generation_succeeded(_event(_good_payload()))

    assert await enqueue_idempotently(service, request) is True
    assert await enqueue_idempotently(service, request) is False
    assert len(repo.rows) == 1


async def test_enqueue_propagates_non_idempotent_failure() -> None:
    """非幂等冲突的错误必须上抛：否则真正入队失败会被当成功 ack。"""

    class ExplodingService:
        async def enqueue(self, **kwargs: Any) -> None:
            from packages.errors.error_codes import ErrorRegistry

            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="boom")

    request = parse_generation_succeeded(_event(_good_payload()))
    with pytest.raises(DomainError, match="boom"):
        await enqueue_idempotently(ExplodingService(), request)


# ---- 处理器接线 ----------------------------------------------------------- #


async def test_handler_parses_then_enqueues() -> None:
    seen: list[GenerationSucceeded] = []

    async def enqueue(request: GenerationSucceeded) -> bool:
        seen.append(request)
        return True

    handler = build_generation_handlers(enqueue)["GENERATION_SUCCEEDED"]
    await handler(_event(_good_payload()))

    assert len(seen) == 1
    assert seen[0].generation_task_id == _TASK_ID


async def test_handler_does_not_swallow_malformed() -> None:
    """违约事件必须冒泡给 relay，转成一次失败尝试（最终进死信）。"""

    async def enqueue(request: GenerationSucceeded) -> bool:  # pragma: no cover
        raise AssertionError("不应被调用：载荷已校验失败")

    handler = build_generation_handlers(enqueue)["GENERATION_SUCCEEDED"]
    payload = _good_payload()
    payload.pop("generation_task_id")

    with pytest.raises(MalformedEventError):
        await handler(_event(payload))


def test_handlers_empty_without_dependencies() -> None:
    """没有 QC 会话工厂时不注册处理器：事件保持未投递、积压可见。

    注册一个只会失败的处理器更糟——它会把每条事件推成死信。
    """
    assert build_handlers() == {}
