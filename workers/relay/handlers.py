"""Outbox 事件处理器注册表（按 ``event_type`` 细分的下游接入点）。

为什么生成成功入队必须走事件而不是直接调用
------------------------------------------
生成 Worker 曾在自己的事务里 finish 之后、commit 之前，用**独立的 QC 会话**
直接写 ``qc_governance.qc_tasks`` 并立刻提交。那个顺序是：QCTask 先落库，
GenerationTask 后提交。一旦生成域提交失败，库里就留下一条指向不存在生成任务的
待检项——而 ``qc_tasks.generation_task_id`` 是跨域引用、没有物理外键，数据库
不会替我们拦住它。

改成事件后顺序天然正确：生成域把 ``GENERATION_SUCCEEDED`` 与终态**同事务**
写入 outbox；事件存在 ⟺ 终态已提交。relay 消费才入队，因此不可能出现
「待检项先于生成成功」。投递是**至少一次**，入队按
``generation_task_id`` 唯一约束幂等（见 ``QCTaskService.enqueue``）——
重复投递不会产生第二条待检项。

为什么处理器自己开会话
----------------------
relay 以 ``outbox_relay_role`` 连接，只持有 outbox schema 的 SELECT + UPDATE，
没有业务 schema 写权限（ADR-R06 方案 B 的刻意选择）。因此处理器不能用 relay
那个 session 写 qc_tasks；它用注入的 QC 会话工厂，在自己的事务里完成入队，
再返回让 relay ack。这正是 outbox「消费方自己幂等」的固有语义。

未注册处理器的事件保持未投递（不 ack），积压可见——宁可报警，也不谎报已投递。

可测性：载荷解析与幂等收敛都是纯函数/纯协程，处理器只做「解析 → 入队」，
入队实现由调用方注入（生产是 SQL，测试是内存替身）。这样判据测的是语义，
不是「SQL 恰好能跑」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.logging import get_logger
from packages.shared.rls import set_tenant_context
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService
from workers.relay.asset_registration import build_asset_registration_handlers
from workers.relay.consumer import EventHandler, OutboxEvent

logger = get_logger(__name__)


@dataclass(frozen=True)
class GenerationSucceeded:
    """一条 ``GENERATION_SUCCEEDED`` 事件里入队所需的最小事实集合。"""

    tenant_id: UUID
    generation_task_id: str
    asset_version_id: str
    quality_profile_version: str
    project_id: str
    shot_id: str


class MalformedEventError(ValueError):
    """事件载荷不满足消费契约。

    刻意**不**继承 DomainError：它不是业务拒绝，而是发出方违约。
    让 relay 按失败处理并最终推进死信，问题才会出现在积压/死信指标里；
    若当成功 ack 掉，缺字段的事件会永久消失且无人知晓。
    """


def parse_generation_succeeded(event: OutboxEvent) -> GenerationSucceeded:
    """解析并校验事件载荷；缺字段即抛 ``MalformedEventError``。

    校验放在消费端而不是只放发出端：发出端可能被绕过（直接写 outbox 的脚本、
    历史事件、将来的第二种发出方），而消费端是必经之路。
    """
    payload = event.payload
    generation_task_id = str(payload.get("generation_task_id") or "").strip()
    asset_version_id = str(payload.get("asset_version_id") or "").strip()
    quality_profile_version = str(payload.get("quality_profile_version") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    shot_id = str(payload.get("shot_id") or "").strip()

    if not generation_task_id:
        raise MalformedEventError(
            "GENERATION_SUCCEEDED 缺少 generation_task_id，无法确定幂等键"
        )
    if not asset_version_id:
        raise MalformedEventError(
            "GENERATION_SUCCEEDED 缺少 asset_version_id，无法确定待检资产"
        )
    if not quality_profile_version:
        raise MalformedEventError(
            "GENERATION_SUCCEEDED 缺少 quality_profile_version，无法确定检测口径"
        )
    if not project_id:
        raise MalformedEventError(
            "GENERATION_SUCCEEDED 缺少 project_id，无法建立待检项的归属"
        )

    return GenerationSucceeded(
        tenant_id=event.tenant_id,
        generation_task_id=generation_task_id,
        asset_version_id=asset_version_id,
        quality_profile_version=quality_profile_version,
        project_id=project_id,
        shot_id=shot_id,
    )


async def enqueue_idempotently(service: Any, request: GenerationSucceeded) -> bool:
    """把一次成功幂等地转成 QC 待检项。返回是否真的新建了待检项。

    幂等有两条判据，缺一不可：先查（便宜快路径）与唯一约束（并发兜底）。
    ``QCTaskService.enqueue`` 内部已做先查；并发下两个 relay 副本可能都通过
    先查，此时唯一约束冲突会被收敛成 ``IDEMPOTENCY_CONFLICT``。这里把它翻译
    成「已入队 = 投递成功」，其余 DomainError 照常上抛。

    反面：把 ``IDEMPOTENCY_CONFLICT`` 当失败，会让重复投递的事件被反复重试直到
    进死信；把其它错误当成功，会让真正入队失败的产物永久失检。
    """
    try:
        await service.enqueue(
            project_id=request.project_id,
            shot_id=request.shot_id,
            asset_version_id=request.asset_version_id,
            generation_task_id=request.generation_task_id,
            quality_profile_version=request.quality_profile_version,
            created_by="outbox-relay",
            tenant_id=request.tenant_id,
        )
    except DomainError as exc:
        if exc.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT:
            logger.info(
                "qc_enqueue_idempotent_skip",
                generation_task_id=request.generation_task_id,
            )
            return False
        raise
    return True


EnqueueFn = Callable[[GenerationSucceeded], Awaitable[bool]]


def make_sql_enqueue(qc_sessionmaker: Any) -> EnqueueFn:
    """生产用入队实现：QC 角色独立会话，事务提交后才算入队成功。"""

    async def enqueue(request: GenerationSucceeded) -> bool:
        async with qc_sessionmaker() as session:
            await set_tenant_context(session, request.tenant_id)
            service = QCTaskService(
                SqlAlchemyQCTaskRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            created = await enqueue_idempotently(service, request)
            # 幂等跳过时也提交：本事务没有新写入，提交只是释放连接与行锁。
            await session.commit()
            return created

    return enqueue


def build_generation_handlers(enqueue: EnqueueFn) -> dict[str, EventHandler]:
    """生成域事件的处理器集合（入队实现由调用方注入）。"""

    async def handle_generation_succeeded(event: OutboxEvent) -> None:
        request = parse_generation_succeeded(event)
        await enqueue(request)

    return {"GENERATION_SUCCEEDED": handle_generation_succeeded}


def build_handlers(
    *,
    qc_sessionmaker: Any | None = None,
    production_planning_sessionmaker: Any | None = None,
    asset_sessionmaker: Any | None = None,
) -> dict[str, EventHandler]:
    """按可用依赖装配处理器集合。

    依赖缺省时**不注册**对应处理器：事件保持未投递并让积压上涨，
    比注册一个只能失败的处理器更诚实（后者会把每条事件推成死信）。
    """
    handlers: dict[str, EventHandler] = {}
    if qc_sessionmaker is not None:
        handlers.update(build_generation_handlers(make_sql_enqueue(qc_sessionmaker)))
    if production_planning_sessionmaker is not None and asset_sessionmaker is not None:
        handlers.update(
            build_asset_registration_handlers(
                production_planning_sessionmaker=production_planning_sessionmaker,
                asset_sessionmaker=asset_sessionmaker,
            )
        )
    return handlers


HANDLERS: dict[str, EventHandler] = build_handlers()


__all__ = [
    "HANDLERS",
    "EnqueueFn",
    "GenerationSucceeded",
    "MalformedEventError",
    "build_generation_handlers",
    "build_handlers",
    "enqueue_idempotently",
    "make_sql_enqueue",
    "parse_generation_succeeded",
]
