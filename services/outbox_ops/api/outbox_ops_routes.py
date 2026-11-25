"""Outbox 运维路由：死信查询与人工重放（D6-9）。

``GET /outbox/dead-letters`` 是**只读**的死信集合入口；
``POST /outbox/dead-letters/{event_id}/replay`` 是唯一的**写**入口。

为什么查询端点迟于重放端点
--------------------------
重放先落地，但当时只写了「能不能重放」，没写「怎么找到要重放的」：运维只能
手工 SQL 查 ``dead_lettered_at IS NOT NULL``。这留下一个可见的缺口——契约里
没有查询动作，于是「死信可查」仍然只是注释承诺。本文件补齐它，并且把查询与
处置拆成两个动作、两个席位语义：**看**和**放行**不是同一件事。

为什么身份只取 Principal
------------------------
动作席位 ``OUTBOX.DEAD_LETTER.READ`` / ``OUTBOX.DEAD_LETTER.REPLAY`` 是专项
席位（与 relay 的投递身份、业务域的写入身份都不同）。``operator`` 落审计时取
``principal.subject_id``：若允许请求体自报操作者，审计行记的就是自称，而不是
事实。

为什么租户只取 Principal
------------------------
``tenant_id`` 不得来自请求体或查询参数：跨租户重放别人的死信，等于替别的租户
「复活」事件；跨租户列举死信则直接泄露别处的事件元数据。连接以运维角色连库、
RLS 按 Principal 的租户生效，跨租户行根本不可见。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.metrics import OUTBOX_DEAD_LETTER_REPLAYS_TOTAL
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_outbox_ops_tenant_uow,
    require_role,
)
from services.outbox_ops.repository.dead_letter_repo import (
    SqlAlchemyDeadLetterRepository,
)
from services.outbox_ops.service.dead_letter_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DeadLetterQueryService,
    DeadLetterReplayService,
)

router = APIRouter(prefix="/outbox/dead-letters", tags=["outbox"])

_REASON_MAX = 500
_CURSOR_MAX = 256


class ReplayDeadLetterRequest(BaseModel):
    """重放请求：只说「为什么」。

    ``extra="forbid"`` 是这里唯一的防呆手段：静默忽略 ``operator`` / ``tenant_id``
    之类的字段，会让人以为自己指定了操作者或租户，实际被丢弃——比报错更危险。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=_REASON_MAX)


class DeadLetterItem(BaseModel):
    """列表里的一条死信（运维元数据，不含 payload）。

    不含 ``payload`` 是权限边界，不是省事：payload 里有业务数据与跨域引用，
    把它们整批吐到运维列表里，等于把「可读运维元数据」悄悄放大成「可读业务
    事件内容」。要排查内容请按 event_id 去业务域查，那条路径有各自的授权。
    """

    event_id: str
    event_type: str
    attempts: int
    dead_lettered_at: datetime
    occurred_at: datetime


class DeadLetterListResponse(BaseModel):
    """一页死信；``next_cursor`` 为空表示没有下一页。"""

    items: list[DeadLetterItem]
    next_cursor: str | None


def _service(uow: UnitOfWork) -> DeadLetterReplayService:
    """装配写服务（仓储只依赖会话，不依赖路由；单测可直接换掉仓储）。"""
    return DeadLetterReplayService(SqlAlchemyDeadLetterRepository(uow.session))


def _query_service(uow: UnitOfWork) -> DeadLetterQueryService:
    return DeadLetterQueryService(SqlAlchemyDeadLetterRepository(uow.session))


@router.get("", response_model=DeadLetterListResponse)
async def list_dead_letters(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    event_type: str | None = Query(None, min_length=1, max_length=64),
    cursor: str | None = Query(None, min_length=1, max_length=_CURSOR_MAX),
    principal: Principal = Depends(require_role("OUTBOX.DEAD_LETTER.READ")),
    uow: UnitOfWork = Depends(get_outbox_ops_tenant_uow),
) -> DeadLetterListResponse:
    """列出一页死信（按进入死信时间倒序），供运维找到要处置的事件。

    分页用游标而不是页码：死信是「正在被处置的队列」，翻页期间行会消失，
    OFFSET 分页会因此漏行。游标就是上一页最后一条的
    ``(dead_lettered_at, event_id)``，原样回传即可。

    只读，因此不 ``commit``：没有要提交的东西，而显式提交会让「这条路径不写库」
    这个事实变得不明确。
    """
    page = await _query_service(uow).list_dead_letters(
        tenant_id=principal.tenant_id,
        limit=limit,
        event_type=event_type,
        cursor=cursor,
    )
    return DeadLetterListResponse(
        items=[
            DeadLetterItem(
                event_id=str(item.event_id),
                event_type=item.event_type,
                attempts=item.attempts,
                dead_lettered_at=item.dead_lettered_at,
                occurred_at=item.occurred_at,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


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
    # 提交成功之后才计数。放在服务层看似更"领域内"，但服务返回时事务还没提交：
    # 提交失败会让这次重放整体回滚，而计数器不会回滚——那样「重放次数」会把
    # 一次失败记成一次成功，且这个错误没有任何地方能纠正。
    OUTBOX_DEAD_LETTER_REPLAYS_TOTAL.labels(event_type=result.event_type).inc()
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
