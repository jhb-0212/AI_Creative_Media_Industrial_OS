"""交付清单审批事件消费 worker（D4）。

为什么需要独立 worker 而不是塞进 outbox relay：
- relay 用 OUTBOX_RELAY 角色，只能读事件 + 回写投递状态列，**写不了业务表**
  （workers/relay/__main__.py 的安全边界）；
- 消费审批结论要写 delivery_manifests 的 status，必须用 ASSET 角色，
  因此独立成一个 worker，与 relay 共用 outbox 但各用各的数据库角色。

租户隔离与项目范围（R4-1 根治）：
- 消费要读 ``asset_service.delivery_manifests`` 这张**项目级资源表**，其
  restrictive project 策略要求 ``app.current_org_id`` + ``app.current_projects``
  同时注入（只设 tenant 会看到 0 行）。因此 worker 不能只调
  ``set_tenant_context``，必须用 ``set_request_context`` 注入**完整**上下文。
- 项目范围与组织 id 来自**受信编排配置**（``DeliveryConsumerContext``），
  由 ``__main__`` 从环境变量解析后注入，**绝不从事件 payload 自称**——事件是
  审批引擎写出的业务数据，不是授权凭据；信任它会重开"伪造 project 范围"的口子。
- 跨租户枚举能力（``outbox.list_active_tenants()``）只授给 relay 角色；本
  worker 不擅自扩大资产域权限，而是由调用方显式给出每个租户的消费上下文。

幂等与失败分类（R4-1/R4-3 根治）：
- 消费以清单状态为准，不是以"事件是否投递"为准。同一事件重放时清单已
  VALIDATED 且 audit_ref 一致 → 领域层直接返回（不计失败）；
- 已由**另一**审批推进到终态（STATE_TRANSITION_DENIED）→ 幂等跳过，可静默；
- ``NOT_FOUND`` / ``VALIDATION_ERROR`` / ``CONCURRENT_MODIFICATION`` 是真实
  失败 → 计入 ``failed``、显式 ``rollback()``，不再伪装成"幂等重复"被吞掉。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.logging import get_logger
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.rls import set_request_context
from services.asset_service.repository.sqlalchemy_approval_event_source import (
    SqlAlchemyApprovalEventSource,
)
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.service.delivery_approval_service import (
    _APPROVE_ACTION_KEY,
    _APPROVE_RESOURCE_TYPE,
    DeliveryApprovalService,
)

logger = get_logger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class DeliveryConsumerContext:
    """worker 的受信服务身份上下文（消费一个租户所需的最小授权边界）。

    语义与 ``Principal`` 对齐，但**不是**从 JWT 验签得到：worker 没有请求
    主体，它的"身份"是编排层授予的服务账号身份。因此：

    - ``tenant_id`` / ``organization_id`` / ``project_ids`` 由部署配置注入，
      代表"这个 worker 被授权以该组织/项目范围消费审批事件"；
    - ``project_ids`` 为空集时 fail-closed（一个项目都读不到，绝不默认全部）；
    - 这里刻意不提供"缺省即全部"的便捷构造：越权范围必须显式写出。

    ``subject_id`` 固定为服务账号标识，仅用于审计关联（谁在什么范围内消费）。
    """

    tenant_id: uuid.UUID
    organization_id: uuid.UUID
    project_ids: frozenset[str]

    def to_principal(self) -> Principal:
        """转为 ``Principal`` 以复用 ``set_request_context`` 的唯一注入路径。

        复用而非另写裸 SQL：注入口径只有 ``packages.shared.rls`` 一处，
        worker 不可能写出与 API 不一致的 GUC。
        """
        return Principal(
            subject_id="svc_delivery_consumer",
            subject_type=SubjectType.SERVICE,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            project_ids=self.project_ids,
        )


@dataclass
class DeliveryConsumeStats:
    """单轮消费统计。"""

    tenants_scanned: int = 0
    events_seen: int = 0
    promoted: int = 0
    skipped: int = 0
    failed: int = 0


def _is_idempotent_reject(exc: DomainError) -> bool:
    """只有"终态已被推进（幂等重复）"可以静默跳过。

    其它 DomainError（NOT_FOUND / VALIDATION_ERROR / CONCURRENT_MODIFICATION）
    都是真实失败，必须显式暴露并 rollback，而不是伪装成幂等重复被吞掉——
    否则 R4-1 那种"事件被看到、清单永远不动、监控不报警"的静默停摆会重演。
    """
    return exc.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


async def consume_tenant(
    sessionmaker: SessionMaker,
    context: DeliveryConsumerContext,
    stats: DeliveryConsumeStats,
) -> None:
    """在单个租户的**完整 RLS 上下文**内消费一轮（该租户一个事务）。

    上下文必须是受信的 ``DeliveryConsumerContext``：只设 tenant 会让项目级
    资源表在 restrictive project 策略下判否（0 行），消费静默空转。
    """
    async with sessionmaker() as session:
        await set_request_context(session, context.to_principal())
        source = SqlAlchemyApprovalEventSource(session)
        service = DeliveryApprovalService(
            SqlAlchemyDeliveryManifestRepository(session),
            source,
            SqlAlchemyOutboxWriter(session),
        )
        events = await source.list_completed(
            action_key=_APPROVE_ACTION_KEY,
            resource_type=_APPROVE_RESOURCE_TYPE,
        )
        for event in events:
            stats.events_seen += 1
            if event.decision != "APPROVED":
                stats.skipped += 1
                continue
            try:
                await service.consume_approval(manifest_id=event.resource_id)
            except DomainError as exc:
                if _is_idempotent_reject(exc):
                    # 终态已被另一审批推进：幂等，可静默。
                    stats.skipped += 1
                else:
                    # 真实失败：显式回滚本事务，避免把半截状态 commit 掉。
                    stats.failed += 1
                    await session.rollback()
                    logger.error(
                        "delivery_consumer_event_failed",
                        manifest_id=str(event.resource_id),
                        event_id=str(event.event_id),
                        error_code=exc.error_code.code,
                        detail=exc.detail,
                    )
            else:
                stats.promoted += 1
        await session.commit()


async def run_once(
    sessionmaker: SessionMaker, *, contexts: tuple[DeliveryConsumerContext, ...]
) -> DeliveryConsumeStats:
    """扫描给定租户上下文，消费所有已通过的交付审批事件。"""
    stats = DeliveryConsumeStats()
    for context in contexts:
        stats.tenants_scanned += 1
        try:
            await consume_tenant(sessionmaker, context, stats)
        except Exception:
            # 单租户失败不影响其它租户；不吞掉堆栈（可观测性）。
            stats.failed += 1
            logger.exception(
                "delivery_consumer_tenant_failed",
                tenant_id=str(context.tenant_id),
            )
    return stats


__all__ = [
    "DeliveryConsumeStats",
    "DeliveryConsumerContext",
    "consume_tenant",
    "run_once",
]
