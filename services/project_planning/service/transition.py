"""项目域状态推进的唯一入口（ADR-R23 §D2-b）。

为什么必须收敛成一个函数（根因，不是为抽象而抽象）
--------------------------------------------------
D2 之前，本域有 **3 条彼此独立**的写入路径（项目 / 集数 / 剧本），每条都把
「守卫 → 乐观锁落库 → 写事件」这四步复制一遍。当时没有代价，因为四步里没有
哪一步需要在**所有**路径上保持一致。

D2 引入了第一个这样的步骤：写状态迁移事实行（`audit.status_transitions`）。
它要求「有推进 ⇒ 有事实行」，而完整性不可能靠「记得在第 4 个地方也加一行」保证——
漏一处，缺口是静默的：状态改了、事件发了、审计查不到，且没有任何闸门会红。

所以顺序必须是：先把这个域收敛成一条路径，再在唯一路径上挂记录点。
若反过来（在 3 处各加一次写入），就是把「同一规则多份实现」这个缺陷复制 3 份。

边界说明：本函数**不是**新的 Manager/Service 层。它没有状态、没有缓存、不做业务
判断，只承载那一条固定序列；守卫求值与合法边判定仍在各域 `transition_to()` 与
`domain/state_machine.py` 里（单一事实源不变）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.status_transition import (
    TransitionEvent,
    TransitionFact,
    transition_audit_ref,
)
from services.project_planning.repository._versioning import ConcurrentModificationError

# 冲突策略必须显式声明：两种语义真实存在，不能靠注释表达。
# - "raise"：并发冲突是错误（项目/集数状态变更的默认语义）。
# - "skip" ：并发冲突表示「终态已由他人达成」，幂等跳过（剧本版本取代）。
ConflictPolicy = Literal["raise", "skip"]

# 无具体主体的系统推进（如后台补偿）标注为系统主体，而不是编造一个人。
# 目前没有调用方走到这里——保留它是为了让「actor 缺失」有明确语义，
# 而不是让 NOT NULL 列在运行时抛一个看不懂的数据库错误。
_SYSTEM_ACTOR = "system"


class VersionedStatusEntity(Protocol):
    """推进函数对实体的最小要求（结构化类型，不引入共享基类）。"""

    status: Any
    entity_version: int
    entity_id: str

    def transition_to(self, target: Any, audit_ref: str) -> Any: ...


class StatusRepository(Protocol):
    async def save(self, entity: Any, expected_version: int) -> Any: ...


class OutboxRecorder(Protocol):
    def record_transition(
        self, *, fact: TransitionFact, event: TransitionEvent
    ) -> UUID: ...


async def advance_status(
    entity: VersionedStatusEntity,
    target_status: Any,
    *,
    repository: StatusRepository,
    outbox_writer: OutboxRecorder,
    aggregate_type: str,
    event_type: str,
    tenant_id: UUID,
    payload: Mapping[str, Any],
    resource_type: str,
    owner_action: str,
    conflict_detail: str,
    actor_field: str | None = None,
    actor: str | None = None,
    organization_id: UUID | None = None,
    authority_ref: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    on_conflict: ConflictPolicy = "raise",
) -> bool:
    """执行一次状态推进并原子写入事件，返回是否真的推进了。

    ``payload`` 只传**该事件特有**的键。``from_status`` / ``to_status`` /
    ``tenant_id`` 由本函数统一补齐：这三个键每一件状态事件都必须有，散在各调用点
    写就等于允许某一处漏掉，而漏掉的那一处不会被任何闸门发现。

    ``actor_field`` 是操作者在**该事件契约里**的键名（`PROJECT_STATUS_CHANGED`
    用 `changed_by`，`SCRIPT_SUPERSEDED` 无此字段——它的「谁」是 `superseded_by`
    这个实体引用）。刻意不做「统一改名成 actor」：那会改动已登记的事件 payload 契约，
    也会逼调用方为没有主体的事件编造一个主体。没有主体时传 `None`，本函数不注入。

    ``on_conflict="skip"`` 时并发冲突返回 ``False``（调用方据此幂等跳过），
    其余异常照常上抛——幂等跳过只对「终态已达成」这一种冲突成立。

    ``resource_type`` / ``owner_action`` 是迁移事实的必填构件：前者标识被推进的
    对象（Schema 键名），后者是拥有该转移边的 action_key。二者都由调用方给——
    本函数不做映射推断，推断出来的归属是猜的，不是事实。
    """
    previous_status = entity.status
    expected_version = entity.entity_version
    # 事实 id 必须先于迁移生成：audit_ref 已降级为「指向本次事实的指针」，
    # 若等写完事实再回填，迁移那一刻就只有自由串可写（ADR-R23 §D2-c）。
    transition_id = uuid4()
    entity.transition_to(target_status, transition_audit_ref(transition_id))
    try:
        await repository.save(entity, expected_version)
    except ConcurrentModificationError as exc:
        if on_conflict == "skip":
            return False
        raise DomainError(
            ErrorRegistry.CONCURRENT_MODIFICATION, detail=conflict_detail
        ) from exc

    outbox_writer.record_transition(
        fact=TransitionFact(
            transition_id=transition_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=entity.entity_id,
            entity_version=entity.entity_version,
            from_status=_status_value(previous_status),
            to_status=_status_value(target_status),
            owner_action=owner_action,
            actor_subject=actor or _SYSTEM_ACTOR,
            authority_ref=authority_ref,
            evidence_refs=evidence_refs,
        ),
        event=TransitionEvent(
            aggregate_type=aggregate_type,
            aggregate_id=entity.entity_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload={
                **payload,
                **({actor_field: actor} if actor_field else {}),
                "from_status": _status_value(previous_status),
                "to_status": _status_value(target_status),
                "tenant_id": str(tenant_id),
            },
        ),
    )
    return True


def _status_value(status: Any) -> Any:
    """状态值统一取 ``.value``；已是字符串时原样返回。

    剧本取代路径此前直接传过 ``previous_status.value``（str），另两处传枚举。
    收敛后由本函数归一，避免调用方各自决定序列化形态。
    """
    return getattr(status, "value", status)


__all__ = [
    "ConflictPolicy",
    "OutboxRecorder",
    "StatusRepository",
    "VersionedStatusEntity",
    "advance_status",
]
