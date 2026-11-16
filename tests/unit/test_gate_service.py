"""Gate 裁决用例单测（内存 fake，无数据库）。

验证"事实记录"这条主线的四个性质：

- **幂等**：同一实例 + 同一快照 + 同一规则版本 + 同一裁决只记一次、只发一次事件；
- **冲突不是改写**：内容不同却复用幂等键 → 409，不能借重放覆盖已发生的裁决；
- **并发同判定**：仓储抛 DuplicateEntityError 后回查，与快速路径用同一比对函数；
- **事件反映事实**：APPROVED / REJECTED / RETURNED 是三个事件名，否决不得
  混进 Gate 通过率的分子（审计污染比缺事件更难发现）。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import SubjectType
from services.qc_governance.domain.enums import GateDecisionValue, GateKey
from services.qc_governance.domain.gate_decision import GateDecision
from services.qc_governance.repository.gate_repo import GateDecisionRepository
from services.qc_governance.service.gate_service import GateService
from tests.unit.test_asset_service import FakeOutboxWriter, _principal

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d4b1")
_PRINCIPAL = _principal("gate-decider", "导演")


def _fields(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "project_id": "proj-gate-service",
        "gate_key": GateKey.GATE_4,
        "gate_instance_id": "instance-1",
        "decision": GateDecisionValue.APPROVED,
        "constraint_snapshot_id": "snapshot-1",
        "ruleset_version": 3,
        "entry_criteria": "entry",
        "required_artifacts": "artifacts",
        "reviewers": ["gate-decider"],
        "reason_code": "PASS",
        "comment": "通过",
        "evidence_refs": ["evidence-1"],
        "scope": {"level": "project"},
        "principal": _PRINCIPAL,
        "entity_id": "decision-1",
    }
    values.update(overrides)
    return values


class FakeGateRepository(GateDecisionRepository):
    def __init__(self) -> None:
        self.rows: dict[str, GateDecision] = {}

    async def add(self, decision: GateDecision) -> None:
        key = decision.idempotency_key()
        if decision.entity_id in self.rows or any(
            row.idempotency_key() == key for row in self.rows.values()
        ):
            raise DuplicateEntityError(f"entity_id={decision.entity_id}")
        self.rows[decision.entity_id] = replace(decision)

    async def get_by_id(self, entity_id: str) -> GateDecision | None:
        row = self.rows.get(entity_id)
        return replace(row) if row is not None else None

    async def find_by_idempotency_key(
        self,
        *,
        gate_instance_id: str,
        constraint_snapshot_id: str,
        ruleset_version: int,
        decision: str,
    ) -> GateDecision | None:
        for row in self.rows.values():
            if row.idempotency_key() == (
                gate_instance_id,
                constraint_snapshot_id,
                ruleset_version,
                decision,
            ):
                return replace(row)
        return None

    async def list_by_instance(self, gate_instance_id: str) -> list[GateDecision]:
        rows = [
            replace(row)
            for row in self.rows.values()
            if row.gate_instance_id == gate_instance_id
        ]
        return sorted(rows, key=lambda r: (r.decided_at, r.entity_id))


class _ConcurrentGateRepository(FakeGateRepository):
    """前两次回查看不到记录，add 才发现幂等键已存在（模拟并发重叠）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0

    async def get_by_id(self, entity_id: str) -> GateDecision | None:
        self.lookups += 1
        if self.lookups == 1:
            return None
        return await super().get_by_id(entity_id)

    async def find_by_idempotency_key(self, **kwargs: object) -> GateDecision | None:
        self.lookups += 1
        if self.lookups <= 2:
            return None
        return await super().find_by_idempotency_key(**kwargs)  # type: ignore[arg-type]


def _service(repo: GateDecisionRepository | None = None):
    gates = repo or FakeGateRepository()
    outbox = FakeOutboxWriter()
    return GateService(gates, outbox), gates, outbox


async def test_decide_records_fact_and_emits_event() -> None:
    service, gates, outbox = _service()
    record = await service.decide(**_fields())

    assert record.entity_id == "decision-1"
    assert record.decided_by == "gate-decider"
    assert gates.rows["decision-1"].decision is GateDecisionValue.APPROVED
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["event_type"] == "PRODUCTION_GATE_APPROVED"
    assert event["aggregate_type"] == "GateDecision"
    assert event["payload"]["decision"] == "APPROVED"
    assert event["payload"]["decided_by"] == "gate-decider"
    assert event["payload"]["evidence_refs"] == ["evidence-1"]


@pytest.mark.parametrize(
    ("decision", "event_type"),
    [
        (GateDecisionValue.REJECTED, "PRODUCTION_GATE_REJECTED"),
        (GateDecisionValue.RETURNED, "PRODUCTION_GATE_RETURNED"),
    ],
)
async def test_reject_and_return_emit_distinct_events(
    decision: GateDecisionValue, event_type: str
) -> None:
    service, _, outbox = _service()
    await service.decide(**_fields(decision=decision, evidence_refs=[]))
    assert outbox.events[0]["event_type"] == event_type


async def test_replay_with_same_entity_id_is_idempotent() -> None:
    service, gates, outbox = _service()
    await service.decide(**_fields())
    again = await service.decide(**_fields())

    assert again.entity_id == "decision-1"
    assert len(gates.rows) == 1
    assert len(outbox.events) == 1


async def test_replay_with_different_id_same_key_is_idempotent() -> None:
    """客户端换了 id 重放同一件事，仍只能有一条事实、一个事件。"""
    service, gates, outbox = _service()
    await service.decide(**_fields(entity_id="decision-1"))
    again = await service.decide(**_fields(entity_id="decision-2"))

    assert again.entity_id == "decision-1"
    assert len(gates.rows) == 1
    assert len(outbox.events) == 1


async def test_same_key_different_content_is_conflict() -> None:
    """改写已发生的裁决不得借幂等键通过（否则重复提交成了修改后门）。"""
    service, gates, outbox = _service()
    await service.decide(**_fields())
    with pytest.raises(DomainError) as exc:
        await service.decide(**_fields(entity_id="decision-2", reason_code="OTHER"))
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
    assert len(gates.rows) == 1
    assert len(outbox.events) == 1


async def test_agent_principal_is_rejected_without_writing() -> None:
    """授权层之外的第二道：Agent 主体即使走到服务层也不产生记录或事件。"""
    service, gates, outbox = _service()
    agent = replace(
        _PRINCIPAL,
        subject_id="agent-1",
        subject_type=SubjectType.AGENT,
    )
    with pytest.raises(DomainError) as exc:
        await service.decide(**_fields(principal=agent, reviewers=["agent-1"]))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not gates.rows
    assert not outbox.events


async def test_concurrent_duplicate_with_same_content_is_idempotent() -> None:
    """并发冲突路径回查后与快速路径共用同一判定（不允许绕过内容比对）。"""
    repo = _ConcurrentGateRepository()
    await repo.add(_create_domain())
    service, gates, outbox = _service(repo)
    result = await service.decide(**_fields(entity_id="decision-2"))

    assert result.entity_id == "decision-1"
    assert len(gates.rows) == 1
    assert len(outbox.events) == 0  # 并发回查命中既有事实，不重复发事件


async def test_concurrent_duplicate_without_existing_is_retryable() -> None:
    """唯一键冲突但回查不到记录：报可重试错误，不伪造成功。"""
    class _PhantomDuplicate(FakeGateRepository):
        async def add(self, decision: GateDecision) -> None:
            raise DuplicateEntityError("phantom")

    service, _, outbox = _service(_PhantomDuplicate())
    with pytest.raises(DomainError) as exc:
        await service.decide(**_fields())
    assert exc.value.error_code is ErrorRegistry.CONCURRENT_MODIFICATION
    assert not outbox.events


async def test_get_missing_is_not_found() -> None:
    service, _, _ = _service()
    with pytest.raises(DomainError) as exc:
        await service.get_decision("missing")
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND


async def test_history_keeps_rejections_visible() -> None:
    """完整轨迹包含否决与退回，不隐藏过程。"""
    service, _, _ = _service()
    await service.decide(**_fields(entity_id="d-1", decision=GateDecisionValue.REJECTED, evidence_refs=[]))
    await service.decide(**_fields(entity_id="d-2", decision=GateDecisionValue.RETURNED, evidence_refs=[]))
    history = await service.history("instance-1")
    assert [row.decision for row in history] == [
        GateDecisionValue.REJECTED,
        GateDecisionValue.RETURNED,
    ]


def _create_domain() -> GateDecision:
    """给并发测试预置一条已存在的事实（绕开服务，模拟别的请求先落库）。"""
    from services.qc_governance.domain.enums import GateDecisionStatus

    return GateDecision(
        entity_id="decision-1",
        project_id="proj-gate-service",
        gate_key=GateKey.GATE_4,
        gate_instance_id="instance-1",
        decision=GateDecisionValue.APPROVED,
        constraint_snapshot_id="snapshot-1",
        ruleset_version=3,
        entry_criteria="entry",
        required_artifacts="artifacts",
        reviewers=["gate-decider"],
        reason_code="PASS",
        comment="通过",
        evidence_refs=["evidence-1"],
        scope={"level": "project"},
        decided_by="gate-decider",
        created_by="gate-decider",
        tenant_id=_TENANT,
        status=GateDecisionStatus.RECORDED,
    )
