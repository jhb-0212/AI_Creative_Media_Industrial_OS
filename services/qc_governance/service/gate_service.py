"""Gate 决策应用服务（PRODUCTION.GATE.APPROVE）。

职责：把"某位授权人类主体对某个不可变输入快照作出裁决"这件事记成
**不可变事实**，并在同一事务里发审计事件。不负责推进任何下游状态。

为什么这一层没有"校验函数据集"：
- 「Agent 不得自批」「缺证据不得 PASS」两条红线的执行点分别在授权层
  （``risk_level=CRITICAL`` + ``is_agent``）和数据库 CHECK 上；领域层只是
  让错误更早、更可读，不是唯一防线。
- 「裁决不可变」由端口无 save + 数据库只授 SELECT/INSERT + 触发器三层保证。
  服务层再加一个"禁止修改"的分支只会让人以为那是唯一防线。

未在这里做的事（刻意，且属显式债务，见 ADR-R23）：
- 不判断 Gate 是否 STALE：那需要"当前约束快照是哪一版"，依赖 ConstraintPack
  落地；本服务只保证每条裁决都绑定了当时的快照 id，使判定将来可做。
- 不实现多人会签/审批顺序：契约 7.4 要求，但 `reviewers` 目前只是参与人记录，
  不构成"多方签名"证据。拒绝把它包装成会签。
- 不实现 OVERRIDDEN：契约未定义"覆盖了哪一条决策"，领域层已拒绝该取值。
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.qc_governance.domain.enums import GateDecisionValue, GateKey
from services.qc_governance.domain.gate_decision import ENTITY_TYPE, GateDecision
from services.qc_governance.repository.gate_repo import GateDecisionRepository

#: 为什么按裁决值分事件，而不是全部记成契约里那一个 `PRODUCTION_GATE_APPROVED`：
#: 把 REJECTED 记成 "APPROVED" 事件会让"Gate 通过率"这个北极星指标分母里混进
#: 否决记录——审计污染比缺事件更难发现。契约只声明了一个 audit_event，是因为
#: 它按"动作"登记；同一动作产生四种结论是事实，事件名必须能反映事实。
#:
#: 三个事件名刻意以字面量出现在下面的发出分支里，不抽成 dict 再动态取值：
#: 契约闸门靠字面量扫描发现"代码发了未登记事件"。映射表会让扫描器失明，
#: 而失明的闸门比没有闸门更危险——它会给人全部合规的错觉。


class GateService:
    """Gate 裁决用例（单域事务，无跨服务依赖）。"""

    def __init__(
        self,
        gates: GateDecisionRepository,
        outbox: OutboxWriter,
    ) -> None:
        self._gates = gates
        self._outbox = outbox

    async def decide(
        self,
        *,
        project_id: str,
        gate_key: GateKey,
        gate_instance_id: str,
        decision: GateDecisionValue,
        constraint_snapshot_id: str,
        ruleset_version: int,
        entry_criteria: str,
        required_artifacts: str,
        reviewers: list[str],
        reason_code: str,
        comment: str,
        evidence_refs: list[str],
        scope: dict[str, Any],
        principal: Principal,
        entity_id: str | None = None,
    ) -> GateDecision:
        """记录一条 Gate 裁决（幂等：同一实例+快照+规则版本+裁决只记一次）。"""
        new_entity_id = entity_id or uuid.uuid4().hex

        record = GateDecision.record(
            entity_id=new_entity_id,
            project_id=project_id,
            gate_key=gate_key,
            gate_instance_id=gate_instance_id,
            decision=decision,
            constraint_snapshot_id=constraint_snapshot_id,
            ruleset_version=ruleset_version,
            entry_criteria=entry_criteria,
            required_artifacts=required_artifacts,
            reviewers=reviewers,
            reason_code=reason_code,
            comment=comment,
            evidence_refs=evidence_refs,
            scope=scope,
            principal=principal,
        )

        # 快速路径 1：同 entity_id 已存在 → 内容一致算重放，否则是改写事实。
        existing = await self._gates.get_by_id(new_entity_id)
        if existing is not None:
            return _assert_same_content(existing, record)

        # 快速路径 2：幂等键命中（客户端换了 id 重放同一裁决）→ 同样处理。
        same_key = await self._find_by_key(record)
        if same_key is not None:
            return _assert_same_content(same_key, record)

        try:
            await self._gates.add(record)
        except DuplicateEntityError as exc:
            # 并发重复提交：约束拦下后回查，与快速路径共用同一判定。
            concurrent = await self._find_by_key(record)
            if concurrent is not None:
                return _assert_same_content(concurrent, record)
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="Gate 裁决并发写入冲突，请重试",
            ) from exc

        payload = self._decision_payload(record, principal)
        if record.decision is GateDecisionValue.APPROVED:
            self._outbox.record(
                aggregate_type=ENTITY_TYPE,
                aggregate_id=record.entity_id,
                tenant_id=principal.tenant_id,
                event_type="PRODUCTION_GATE_APPROVED",
                payload=payload,
            )
        elif record.decision is GateDecisionValue.REJECTED:
            self._outbox.record(
                aggregate_type=ENTITY_TYPE,
                aggregate_id=record.entity_id,
                tenant_id=principal.tenant_id,
                event_type="PRODUCTION_GATE_REJECTED",
                payload=payload,
            )
        elif record.decision is GateDecisionValue.RETURNED:
            self._outbox.record(
                aggregate_type=ENTITY_TYPE,
                aggregate_id=record.entity_id,
                tenant_id=principal.tenant_id,
                event_type="PRODUCTION_GATE_RETURNED",
                payload=payload,
            )
        else:  # pragma: no cover - 领域层与数据库 CHECK 已拒绝其它取值
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"未知 Gate 裁决取值: {record.decision!r}",
            )
        return record

    def _decision_payload(
        self, record: GateDecision, principal: Principal
    ) -> dict[str, Any]:
        """审计事件正文：只放事实引用与判定依据，不放可变对象快照。"""
        return {
            "gate_decision_id": record.entity_id,
            "project_id": record.project_id,
            "gate_key": record.gate_key.value,
            "gate_instance_id": record.gate_instance_id,
            "decision": record.decision.value,
            "constraint_snapshot_id": record.constraint_snapshot_id,
            "ruleset_version": record.ruleset_version,
            "reason_code": record.reason_code,
            "evidence_refs": list(record.evidence_refs),
            "decided_by": record.decided_by,
            "decided_at": record.decided_at.isoformat(),
            "tenant_id": str(principal.tenant_id),
        }

    async def get_decision(self, entity_id: str) -> GateDecision:
        """读取一条裁决；不可见即 NOT_FOUND（不区分"不存在"与"无权看"）。"""
        decision = await self._gates.get_by_id(entity_id)
        if decision is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"Gate 裁决不存在: {entity_id}"
            )
        return decision

    async def history(self, gate_instance_id: str) -> list[GateDecision]:
        """列出某 Gate 实例的完整裁决轨迹（含否决与退回，不隐藏过程）。"""
        return await self._gates.list_by_instance(gate_instance_id)

    async def _find_by_key(self, record: GateDecision) -> GateDecision | None:
        """按幂等键回查（键的组成由领域对象给出，不在此处重复拼装）。"""
        instance_id, snapshot_id, ruleset_version, decision = record.idempotency_key()
        return await self._gates.find_by_idempotency_key(
            gate_instance_id=instance_id,
            constraint_snapshot_id=snapshot_id,
            ruleset_version=ruleset_version,
            decision=decision,
        )


def _assert_same_content(
    existing: GateDecision, incoming: GateDecision
) -> GateDecision:
    """幂等命中必须是同一件事：内容一致才返回既有裁决。

    内容不同却复用同一 id 或同一幂等键，属于**改写已发生的裁决**——契约要求
    决策是不可变事实，因此一律拒绝。注意这里比对的是事实字段（谁、什么结论、
    什么理由、哪些证据），不是 ``decided_at``：客户端无法指定时间，时间由
    服务端在首次记录时固定。
    """
    if existing.same_content(incoming):
        return existing
    raise DomainError(
        ErrorRegistry.IDEMPOTENCY_CONFLICT,
        detail=(
            f"Gate 实例 {incoming.gate_instance_id} 在同一约束快照"
            f"（{incoming.constraint_snapshot_id}）与规则版本"
            f"（{incoming.ruleset_version}）下已有内容不同的 "
            f"{incoming.decision.value} 裁决；裁决不可改写，如需重新评审"
            "请使用新的约束快照或新的规则版本"
        ),
    )


__all__ = ["GateService"]
