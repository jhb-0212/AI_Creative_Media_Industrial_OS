"""死信人工重放（D6-9）——领域判定与编排。

这一层只回答两个问题：**这条死信现在能不能重放**，以及**重放这件事怎么落纸**。
它不改事件内容、不复制新事件、不直调处理器：重放的全部效果是「把事件从死信态
移出并交回正常领取」，之后的投递仍走 relay 的 claim → handler → ack。

为什么不能复制一条新事件重新投递
--------------------------------
``event_id`` 是业务幂等的支点（下行 handler 按它去重，T2 的回执唯一键也挂在
业务键上）。生成一个新 ``event_id`` 重投，等于告诉下游「这是一件新事」——
同一业务事实会被执行两次。所以重放只动投递状态，不动事件身份。

失败审计与「一次尝试一行」
--------------------------
审计表 append-only（授权层无 UPDATE、触发器拒绝 UPDATE/TRUNCATE），因此**不能**
先写 ``IN_FLIGHT`` 再改写成终值。终值只能在写入时就确定，于是本实现把
「锁行 → 条件清标记 → 写审计(SUCCEEDED)」放进**同一个事务**：事务提交则三件事
一起成立，回滚则一件都没发生。这与「先写审计再清零」的目的相同（重放前的失败
事实不会丢——``attempts`` / ``dead_lettered_at`` 在锁行时就已读入内存），
区别是审计行永远带着真实终值，不需要二次改写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox import DeadLetterReplayModel
from packages.shared.paths import contract_file
from packages.shared.yaml_strict import load_yaml_file
from services.outbox_ops.repository.dead_letter_repo import (
    SqlAlchemyDeadLetterRepository,
)

_EVENT_CATALOG_PATH = contract_file("runtime", "event_catalog.yaml")


@lru_cache(maxsize=1)
def declared_event_types() -> frozenset[str]:
    """契约里登记的 event_type 全集（拒绝重放契约外的类型）。

    为什么不查 relay 的 handler 注册表（D6-3 的原始表述是「该事件类型的 handler
    已注册」）：那份注册表活在 relay 进程里，且构建它需要三个域会话工厂——API
    进程既拿不到、也不该拿到。诚实的替代是查契约：**契约外的事件类型**必然没有
    消费者，重放它只会制造一条永远不会被 ack 的积压。

    契约内的类型若恰好没有 handler（例如依赖缺失导致 relay 少注册了一个），
    replay 的后果也不是「丢事件」——relay 对没有处理器的事件**不 ack**，
    积压会上涨并报警。所以这条判据的实质（不因重放而丢失事件）仍然成立。
    """
    data = load_yaml_file(_EVENT_CATALOG_PATH)
    events = data.get("events") or []
    return frozenset(
        str(item["event_type"]) for item in events if item.get("event_type")
    )


@dataclass(frozen=True)
class ReplayResult:
    """一次成功重放的结果（全部来自数据库事实）。"""

    replay_id: UUID
    event_id: UUID
    event_type: str
    attempts_before: int
    dead_lettered_at_before: datetime


class DeadLetterReplayService:
    def __init__(self, repository: SqlAlchemyDeadLetterRepository) -> None:
        self._repository = repository

    async def replay(
        self,
        *,
        event_id: UUID,
        tenant_id: UUID,
        operator_id: str,
        reason: str,
    ) -> ReplayResult:
        """把一条死信移出死信态并交回正常领取。

        判定顺序刻意如此：先锁行拿到**数据库事实**，再做归属与生命周期判定，
        最后才写。反过来的话，越权或非法状态的请求会先在库里留下审计行，再靠
        回滚抹掉——回滚一旦因为别的原因没生效，就变成「拒绝过的操作其实留了痕」。
        """
        row = await self._repository.lock(event_id)
        if row is None:
            # RLS 让跨租户行不可见，因此「不存在」与「不属于本租户」在这里
            # 是同一个答案。刻意不区分：区分等于告诉调用方「别处有这条事件」。
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"死信事件不存在或不可见: {event_id}",
            )
        if row.tenant_id != tenant_id:
            # 理论上不可达（RLS 已过滤）；保留是为了在 RLS 被误配成 BYPASSRLS
            # 时仍然拒绝，而不是依赖单一防线。
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"死信事件不存在或不可见: {event_id}",
            )
        if row.published_at is not None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="事件已投递成功，重放只用于处置死信（published_at 非空）",
            )
        if row.dead_lettered_at is None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="事件不在死信态：仅死信可被重放（dead_lettered_at 为空）",
            )
        if row.event_type not in declared_event_types():
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"事件类型 {row.event_type} 未在事件契约中登记："
                    "重放它只会制造一条永远不会被消费的积压"
                ),
            )

        cleared = await self._repository.clear_dead_letter(event_id)
        if not cleared:
            # 持锁期间状态被改动只可能来自并发（同一行被另一个重放抢先），
            # 说明「能不能重放」的判定已过期——按冲突拒绝，不重试。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="事件已不在死信态（可能已被他人重放），请重新查询后再试",
            )

        replay = DeadLetterReplayModel(
            replay_id=uuid4(),
            source_event_id=row.event_id,
            tenant_id=row.tenant_id,
            event_type=row.event_type,
            operator=operator_id,
            reason=reason,
            attempts_before=row.attempts,
            dead_lettered_at_before=row.dead_lettered_at,
            result="SUCCEEDED",
            failure_reason=None,
        )
        await self._repository.record(replay)
        return ReplayResult(
            replay_id=replay.replay_id,
            event_id=row.event_id,
            event_type=row.event_type,
            attempts_before=row.attempts,
            dead_lettered_at_before=row.dead_lettered_at,
        )
