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

from collections.abc import Callable, Mapping
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
    """推进函数对实体的最小要求（结构化类型，不引入共享基类）。

    ``transition_to`` 必须接收 ``action``：它是**准入凭据**，不是标签。聚合根内部
    用域的 ``Lifecycle.edge_owned_by`` 判定这条边是否归该动作所有，不属于即拒
    （N-07）。把它做成必填位置参数是刻意的——有默认值就等于允许某个聚合悄悄
    不校验，而那一个聚合不会被任何闸门发现。
    """

    status: Any
    entity_version: int
    entity_id: str

    def transition_to(self, target: Any, audit_ref: str, action: str) -> Any: ...


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
    domain_operation: Callable[[str, str], object] | None = None,
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

    ``domain_operation`` 用于「状态变更同时要写审批链字段」的领域操作
    （剧本锁定的申请人/批准人）。为什么允许它存在：剧本的 ``request_lock`` /
    ``approve_lock`` 此前各自把 ``self.status`` 直接赋值，等于在同一聚合上开了
    第二条改状态的路径——恰是审批语义最需要留痕的两条转移反而没有事实行
    （codex 评审 T1）。把领域操作作为参数传进来，而不是让它自己落库，能让
    「状态 + 附属字段 + 事实 + 事件」仍是同一序列。约束：``domain_operation``
    内部必须经 ``transition_to`` 改状态（闸门 ``data.transition_single_entry``
    同时检查服务层不许直接赋值 ``.status``）。

    它的签名是 ``(audit_ref, action)``：领域操作必须把**同一个** action 转交给
    ``transition_to``，因此「申请锁定」这样的封装不能自己决定走哪条边——边所有权
    由本函数声明的 ``owner_action`` 判定。

    ``resource_type`` / ``owner_action`` 是迁移事实的必填构件：前者标识被推进的
    对象（Schema 键名），后者是拥有该转移边的 action_key。``owner_action`` 在这里
    有两个用途，且是**同一个值**：传给 ``transition_to`` 做准入判定，随后写进
    ``TransitionFact``。整改前它只写事实、不参与准入（N-07），于是审计表记录的
    「谁走的」只是调用方的自称。一个参数两处用途，物理上无法不一致。

    ``organization_id`` 的取法（codex 评审 T3）：能从不传，就从实体自身取
    （``entity.organization_id``）。让调用方「记得传」是把不变量交给记忆——项目
    服务确实漏传过，事实里留下 NULL，而 NULL 的语义是「该资源无组织归属」，
    于是一条组织项目的历史被记成了个人资产的历史。显式传参仍保留，供实体自身
    不携带组织键的资源（集数/剧本/资产版本，其组织经 project 派生）在归属解析
    就位后补齐；这些资源当前落 NULL，属已登记债务（ADR-R23 §4），不是正确终态。
    """
    previous_status = entity.status
    expected_version = entity.entity_version
    # 事实 id 必须先于迁移生成：audit_ref 已降级为「指向本次事实的指针」，
    # 若等写完事实再回填，迁移那一刻就只有自由串可写（ADR-R23 §D2-c）。
    transition_id = uuid4()
    audit_ref = transition_audit_ref(transition_id)
    if domain_operation is None:
        entity.transition_to(target_status, audit_ref, owner_action)
    else:
        # 领域操作自己经 transition_to 改状态，同时写审批链字段（申请人/批准人）。
        # 传 audit_ref 而不是 ID：领域层不解释指针格式（ADR-R23 §D2-c）。
        # 传 owner_action 而不是让领域操作自己挑：准入凭据只有一个来源。
        domain_operation(audit_ref, owner_action)

    # 事实里的 to_status 取聚合的真实结果，并当场核对调用方声明的目标一致。
    # 若两者不一致，写进事实的就是一条谎——审计表里最不该有的东西。
    if entity.status != target_status:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=(
                f"状态推进结果与声明目标不一致：{resource_type}/{entity.entity_id} "
                f"期望 {_status_value(target_status)}，实际 {_status_value(entity.status)}"
            ),
        )

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
            organization_id=_effective_organization_id(entity, organization_id),
            resource_type=resource_type,
            resource_id=entity.entity_id,
            entity_version=entity.entity_version,
            from_status=_status_value(previous_status),
            to_status=_status_value(entity.status),
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
                "to_status": _status_value(entity.status),
                "tenant_id": str(tenant_id),
            },
        ),
    )
    return True


def _effective_organization_id(
    entity: VersionedStatusEntity, explicit: UUID | None
) -> UUID | None:
    """事实里的组织归属：显式参数优先，否则取实体自身的组织键。

    实体没有 ``organization_id`` 属性（集数/剧本/资产版本）时返回 None——
    它们的组织只能经 project 派生，当前落 NULL 属已登记债务。
    """
    if explicit is not None:
        return explicit
    return getattr(entity, "organization_id", None)


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
