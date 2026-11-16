"""GateDecision 领域不变量（不可变事实 + 红线在数据层的可执行表达）。

这里不测"能不能存"，只测"什么样的记录根本不允许被构造出来"：

- **Agent 不得决策**：即使调用方绕开授权层直接构造，领域层仍拒绝；
- **缺证据不得 PASS**：APPROVED 无 evidence_refs 直接拒绝；
- **决策人来自 Principal**：工厂不提供 decided_by 参数，调用方没有填错的机会；
- **裁决不可改写**：状态恒为 RECORDED，没有 transition 方法；
- **OVERRIDDEN 可表示不可进入**：枚举保留契约取值，领域拒绝（无覆盖目标引用）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from services.qc_governance.domain.enums import (
    GateDecisionStatus,
    GateDecisionValue,
    GateKey,
)
from services.qc_governance.domain.gate_decision import ENTITY_TYPE, GateDecision

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d4a1")
_PRINCIPAL = Principal(
    subject_id="gate-decider",
    tenant_id=_TENANT,
    organization_id=_TENANT,
    roles=frozenset({"导演"}),
    project_ids=frozenset({"*"}),
)


def _record(**overrides: object) -> GateDecision:
    fields: dict[str, object] = {
        "entity_id": "gate-1",
        "project_id": "proj-gate",
        "gate_key": GateKey.GATE_4,
        "gate_instance_id": "gate-instance-1",
        "decision": GateDecisionValue.APPROVED,
        "constraint_snapshot_id": "snapshot-1",
        "ruleset_version": 3,
        "entry_criteria": "终检报告齐备",
        "required_artifacts": "PictureLock、版权证据、终检报告",
        "reviewers": ["gate-decider", "qc-lead"],
        "reason_code": "ALL_CHECKS_PASSED",
        "comment": "全部检查通过",
        "evidence_refs": ["qc-final-1", "rights-1"],
        "scope": {"level": "project", "shot": "SH001"},
        "principal": _PRINCIPAL,
    }
    fields.update(overrides)
    return GateDecision.record(**fields)  # type: ignore[arg-type]


def test_record_derives_identity_from_principal() -> None:
    """身份、租户、组织只能来自 Principal；决策主体类型记录为 human。"""
    decision = _record()
    assert decision.decided_by == "gate-decider"
    assert decision.created_by == "gate-decider"
    assert decision.decided_by_subject_type is SubjectType.HUMAN
    assert decision.tenant_id == _TENANT
    assert decision.organization_id == _TENANT
    assert decision.status is GateDecisionStatus.RECORDED
    assert decision.entity_version == 1


def test_approved_without_evidence_is_rejected() -> None:
    """缺证据不得 PASS（红线在领域层的表达）。"""
    with pytest.raises(DomainError) as exc:
        _record(evidence_refs=[])
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert "evidence_refs" in exc.value.detail


@pytest.mark.parametrize("decision", [
    GateDecisionValue.REJECTED,
    GateDecisionValue.RETURNED,
])
def test_reject_and_return_do_not_require_evidence(decision: GateDecisionValue) -> None:
    """否决/退回不强制证据：理由由 reason_code + comment 承载。"""
    record = _record(decision=decision, evidence_refs=[])
    assert record.decision is decision
    assert not record.is_approval


def test_overridden_is_representable_but_not_enterable() -> None:
    """契约取值必须可表示（枚举不缺值），但不可进入（无覆盖目标引用）。"""
    assert GateDecisionValue.OVERRIDDEN in list(GateDecisionValue)
    with pytest.raises(DomainError) as exc:
        _record(decision=GateDecisionValue.OVERRIDDEN)
    assert "不可进入" in exc.value.detail


def test_agent_cannot_be_decider_even_if_calling_domain_directly() -> None:
    """绕开授权层直接构造也要拒绝：身份类型是要落库的事实。"""
    agent = Principal(
        subject_id="agent-1",
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=SubjectType.AGENT,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )
    with pytest.raises(DomainError) as exc:
        _record(principal=agent, reviewers=["agent-1"])
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert "Agent" in exc.value.detail


def test_decider_must_be_listed_in_reviewers() -> None:
    """决策人不在 reviewers = 以他人名义审批。"""
    with pytest.raises(DomainError) as exc:
        _record(reviewers=["someone-else"])
    assert "reviewers" in exc.value.detail


def test_empty_scope_and_invalid_ruleset_are_rejected() -> None:
    """裁决必须说明作用范围与规则版本，否则无法复核。"""
    with pytest.raises(DomainError):
        _record(scope={})
    with pytest.raises(DomainError):
        _record(ruleset_version=0)


def test_status_cannot_be_changed_to_non_recorded() -> None:
    """事实记录没有生命周期；连 dataclasses.replace 都过不了校验。"""
    from dataclasses import replace

    recorded = _record()
    assert recorded.status is GateDecisionStatus.RECORDED
    with pytest.raises(DomainError) as exc:
        replace(recorded, status="RELEASED")  # type: ignore[arg-type]
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR


def test_idempotency_key_is_four_part() -> None:
    """幂等键 = 实例 + 快照 + 规则版本 + 裁决；同一件事重复提交被折叠。"""
    first = _record(entity_id="id-1")
    second = _record(entity_id="id-2")
    assert first.idempotency_key() == second.idempotency_key()
    assert first.idempotency_key() == ("gate-instance-1", "snapshot-1", 3, "APPROVED")
    changed = _record(decision=GateDecisionValue.REJECTED)
    assert changed.idempotency_key() != first.idempotency_key()


def test_same_content_ignores_id_but_detects_fact_changes() -> None:
    """重放同一件事算幂等；理由/证据变了属改写事实，必须报冲突。"""
    first = _record(entity_id="id-1")
    replay = _record(entity_id="id-2")
    assert first.same_content(replay)
    assert not first.same_content(_record(entity_id="id-2", reason_code="OTHER"))
    assert not first.same_content(
        _record(entity_id="id-2", evidence_refs=["different"])
    )


def test_to_dict_matches_contract_field_set() -> None:
    """响应字段集必须与冻结契约逐字一致；内部列不得泄漏。"""
    contract = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "schemas"
            / "object_schemas.schema.json"
        ).read_text(encoding="utf-8")
    )
    defs = contract.get("definitions") or contract.get("$defs")
    required = set(defs["GateDecision"]["required"])
    payload = _record().to_dict()
    assert set(payload) == required
    assert payload["entity_type"] == ENTITY_TYPE
    assert payload["decided_by"] == "gate-decider"
    for internal in ("tenant_id", "organization_id", "decided_by_subject_type"):
        assert internal not in payload
