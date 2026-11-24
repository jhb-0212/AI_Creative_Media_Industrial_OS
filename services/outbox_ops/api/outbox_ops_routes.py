"""Outbox 运维路由：死信人工重放（D6-9）。

``POST /outbox/dead-letters/{event_id}/replay`` 是**唯一的**人工处置入口。
它此前不存在：``workers/relay/consumer.py`` 三处注释声称死信「可人工重放」，
但仓库里没有任何端点、脚本或 CLI——运维只能手工 SQL 改三列，操作者与理由
全部不落纸。这条路由把「谁在什么时候放行了一条本该被丢弃的事件」变成
可审计事实。

为什么身份只取 Principal
------------------------
动作席位 ``OUTBOX.DEAD_LETTER.REPLAY`` 是专项席位（与 relay 的投递身份、
业务域的写入身份都不同）。``operator`` 落审计时取 ``principal.subject_id``：
若允许请求体自报操作者，审计行记的就是自称，而不是事实。

为什么租户只取 Principal
------------------------
``tenant_id`` 同样不得来自请求体：跨租户重放别人的死信，等于替别的租户
「复活」事件。连接以运维角色连库、RLS 按 Principal 的租户生效，跨租户行
根本不可见（应用层表现为 404，而不是 403——不暴露「别处存在这条事件」）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_outbox_ops_tenant_uow,
    require_role,
)
from services.outbox_ops.repository.dead_letter_repo import (
    SqlAlchemyDeadLetterRepository,
)
from services.outbox_ops.service.dead_letter_service import DeadLetterReplayService

router = APIRouter(prefix="/outbox/dead-letters", tags=["outbox"])

_REASON_MAX = 500


class ReplayDeadLetterRequest(BaseModel):
    """重放请求：只说「为什么」。

    ``extra="forbid"`` 是这里唯一的防呆手段：静默忽略 ``operator`` / ``tenant_id``
    之类的字段，会让人以为自己指定了操作者或租户，实际被丢弃——比报错更危险。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=_REASON_MAX)


def _service(uow: UnitOfWork) -> DeadLetterReplayService:
    """装配服务（仓储只依赖会话，不依赖路由；单测可直接换掉仓储）。"""
    return DeadLetterReplayService(SqlAlchemyDeadLetterRepository(uow.session))


@router.post("/{event_id}/replay", status_code=200)
async def replay_dead_letter(
    event_id: str,
    body: ReplayDeadLetterRequest,
    principal: Principal = Depends(require_role("OUTBOX.DEAD_LETTER.REPLAY")),
    uow: UnitOfWork = Depends(get_outbox_ops_tenant_uow),
) -> dict[str, Any]:
    """把一条死信移出死信态并交回正常领取；不复制事件、不直调处理器。

    事务约定（R07）：显式 ``commit``，不依赖依赖退出码——退出码在响应发出之后
    执行，失败时客户端已经拿到 200。审计行与状态变更同事务提交，因此不存在
    「标记清了但没审计」或「审计了但标记没清」的中间态。
    """
    try:
        parsed = UUID(event_id)
    except ValueError:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=f"event_id 必须是 UUID: {event_id}",
        ) from None

    service = _service(uow)
    result = await service.replay(
        event_id=parsed,
        tenant_id=principal.tenant_id,
        operator_id=principal.subject_id,
        reason=body.reason,
    )
    await uow.commit()
    return {
        "replay_id": str(result.replay_id),
        "event_id": str(result.event_id),
        "event_type": result.event_type,
        "attempts_before": result.attempts_before,
        "dead_lettered_at_before": result.dead_lettered_at_before.isoformat(),
        # 重放不等于已投递：事件已交回正常领取，实际投递仍由 relay 的
        # claim → handler → ack 完成。这里如实写「已交回」，不写「已投递」。
        "status": "REQUEUED",
    }
