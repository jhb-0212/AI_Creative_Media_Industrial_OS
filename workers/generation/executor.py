"""生成任务执行环：领取 → 适配器执行 → 产出登记 → 终态推进。

幂等与失败分类（对齐 delivery consumer 的纪律）：
- 领取（PENDING→RUNNING）以乐观版本控制裁决：``CONCURRENT_MODIFICATION``
  表示另一实例已领取 → 幂等跳过，不是失败；
- 租约过期（9.4 Worker 崩溃）→ 另一实例以**谓词 CAS** 接管（状态仍为
  RUNNING）；接管失败同样幂等跳过；
- 适配器异常 → RUNNING→FAILED（终态），登记失败原因到 audit_ref；
- **产出登记失败同样进 FAILED**：登记不上资产 = 这次生成没有被交付的产物，
  记成 SUCCEEDED 会让"任务成功但没有东西"成为合法状态，主链会静静断开；
- 终态写入带租约栅栏：租约已被接管时，旧实例写不进终态（返回 False，
  记 skipped）——被接管的僵尸执行不允许覆盖新执行的结果；
- FAILED 任务**绝不原地复活**：重试以新任务承载（attempt+1，9.4），
  数据库守卫触发器同表拦截直连 SQL 的复活尝试。

四段事务（ADR-R33）——顺序不能反：
1. 领取/接管（generation 域，独立事务）；
2. 适配器执行（**事务外**：供应商调用不可持有数据库连接）；
3. 产出登记（**资产域**，资产域角色 + RLS，独立事务）；
4. 终态（generation 域，独立事务，登记产出对象引用）。

第 3 步必须在第 4 步之前：终态要写入产出对象引用（资产版本号），先终态后
登记会让"成功的任务指向一个不存在的资产"。若第 3 步成功而第 4 步失败，
下一轮重跑会重复登记——幂等由确定性资产版本号保证（见 asset_registrar）。

RLS 上下文：生成任务是项目级资源（restrictive project 策略），worker 用
受信编排上下文（``GenerationExecutorContext``）注入完整租户/组织/项目
范围，绝不从任务行自称范围（与 delivery consumer 同纪律）。

worker 身份：``worker_id`` 必须是**进程内唯一**（hostname:pid）。租约归属
按它区分；两个进程共用同一个 id 会让「谁在执行」失去意义，栅栏也会放行
被接管的旧执行。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.logging import get_logger
from packages.shared.rls import set_request_context
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.lease import lease_deadline
from services.generation_execution.domain.ports import GenerationOutputRegistrar
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.service.generation_service import (
    GenerationTaskLifecycle,
)
from workers.generation.adapter import GenerationAdapter

logger = get_logger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]

#: worker 服务身份（审计引用用，与 DeliveryConsumerContext 同范式）。
ACTOR = "svc_generation_executor"

#: 单轮单上下文最多领取的任务数（防饿死其它上下文，可控节奏）。
BATCH_LIMIT = 16

#: 失败原因在 audit_ref 里的长度上限（列宽 128，前缀留给分类标签）。
_REASON_LIMIT = 96


@dataclass(frozen=True)
class GenerationExecutorContext:
    """worker 的受信服务身份上下文。

    ``project_ids`` 为空集时 fail-closed（一个项目都执行不到，绝不默认全部）。
    组织与项目范围是**授权**，必须来自编排层配置，不能从任务行自称。
    """

    tenant_id: uuid.UUID
    organization_id: uuid.UUID
    project_ids: frozenset[str]
    #: 进程内唯一的 worker 标识（hostname:pid）：租约归属与终态栅栏的依据。
    worker_id: str

    def to_principal(self) -> Principal:
        """转为 ``Principal`` 以复用 ``set_request_context`` 的唯一注入路径。"""
        return Principal(
            subject_id=ACTOR,
            subject_type=SubjectType.SERVICE,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            project_ids=self.project_ids,
        )


@dataclass
class GenerationRoundStats:
    """单轮执行统计。"""

    claimed: int = 0  # 从 PENDING 领取
    reclaimed: int = 0  # 接管过期租约（崩溃 worker 的遗留）
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0  # 并发领取失败 / 接管失败 / 被栅栏挡下的终态写入


async def execute_round(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    adapter: GenerationAdapter,
    stats: GenerationRoundStats,
    *,
    registrar: GenerationOutputRegistrar,
    lease_seconds: float,
) -> GenerationRoundStats:
    """在单个受信上下文内执行一轮（至多 BATCH_LIMIT 个新任务 + 回收一批过期租约）。

    顺序：先批量领取 PENDING，再回收过期租约，最后逐个执行。
    - 领取是**一条** ``FOR UPDATE SKIP LOCKED`` 语句：并发实例分到不重叠的
      批次，不会把同一个"最早 N 条"窗口抢成一个成功、其余空转；
    - 回收针对崩溃 worker 的遗留，同样按批限量，避免一轮被历史积压吃满。

    ``registrar`` 与 ``lease_seconds`` 刻意是关键字必填参数（不给默认值）：
    产出登记是主链闭环的组成部分，租约时长是崩溃恢复目标的载体——允许
    "没登记器也能跑""没租约也能跑"就等于允许静默降级。
    """
    principal = context.to_principal()
    started_at = datetime.now(UTC)
    expires_at = lease_deadline(lease_seconds, started_at)

    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        claimed_ids = await repo.claim_pending(
            actor=ACTOR,
            worker_id=context.worker_id,
            lease_expires_at=expires_at,
            now=started_at,
            limit=BATCH_LIMIT,
        )
        # 两个查询必须在**同一事务**内：RLS 上下文是 SET LOCAL（事务级），
        # commit 之后上下文失效，第二个查询会被 RLS 判否而不是返回空表——
        # 那种"静默零行"会让崩溃恢复悄悄停摆，且看起来像"没有崩溃任务"。
        expired_ids = [
            task.entity_id
            for task in await repo.list_expired_leases(BATCH_LIMIT, now=started_at)
        ]
        await session.commit()

    stats.claimed += len(claimed_ids)
    for task_id in claimed_ids:
        await _execute_owned(
            sessionmaker, context, adapter, task_id, stats, registrar, lease_seconds
        )
    for task_id in expired_ids:
        reclaimed = await _take_over(
            sessionmaker, context, task_id, lease_seconds
        )
        if not reclaimed:
            stats.skipped += 1  # 已被并发接管或已终态：幂等跳过。
            continue
        stats.reclaimed += 1
        await _execute_owned(
            sessionmaker, context, adapter, task_id, stats, registrar, lease_seconds
        )
    return stats


async def _take_over(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
    lease_seconds: float,
) -> bool:
    """接管过期租约（谓词 CAS）；返回 False 表示幂等跳过。

    接管只写数据库行，不改写任何本地快照——执行前一律重读（见 ``_load``）。
    """
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        if task is None or task.status is not GenerationTaskStatus.RUNNING:
            return False
        previous_owner = task.lease_owner
        try:
            await GenerationTaskLifecycle(repo).take_over(
                task,
                actor=ACTOR,
                lease_owner=context.worker_id,
                lease_seconds=lease_seconds,
            )
            await session.commit()
        except DomainError as exc:
            if exc.error_code is ErrorRegistry.CONCURRENT_MODIFICATION:
                # 租约未到期或已被并发接管：不是失败，是别人在执行。
                return False
            raise
    logger.warning(
        "generation_task_lease_reclaimed",
        generation_task_id=task_id,
        worker_id=context.worker_id,
        previous_owner=previous_owner,
    )
    return True


async def _execute_owned(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    adapter: GenerationAdapter,
    task_id: str,
    stats: GenerationRoundStats,
    registrar: GenerationOutputRegistrar,
    lease_seconds: float,
) -> None:
    """执行一个**已归属本实例**的任务：执行 → 登记产出 → 终态。"""
    task = await _load(sessionmaker, context, task_id)
    if task is None or task.status is not GenerationTaskStatus.RUNNING:
        # 取得执行权之后被外部改成终态只可能是运维介入；幂等跳过。
        stats.skipped += 1
        return

    principal = context.to_principal()

    # 1) 适配器执行在事务外：供应商调用（未来）不可持有数据库连接。
    try:
        output = await adapter.execute(task)
    except Exception as exc:
        reason = _format_reason("adapter", exc)
        if await _transition_failed(sessionmaker, context, task_id, reason):
            stats.failed += 1
        else:
            stats.skipped += 1
        logger.warning(
            "generation_task_failed",
            generation_task_id=task_id,
            stage="adapter",
            reason=reason,
        )
        return

    # 2) 产出登记（资产域事务）：登记不上就没有可交付的产物，进 FAILED。
    try:
        registration = await registrar.register(
            task=task, output=output, principal=principal
        )
    except Exception as exc:
        reason = _format_reason("output_registration", exc)
        if await _transition_failed(sessionmaker, context, task_id, reason):
            stats.failed += 1
        else:
            stats.skipped += 1
        logger.error(
            "generation_task_failed",
            generation_task_id=task_id,
            stage="output_registration",
            reason=reason,
            exc_info=True,
        )
        return

    # 3) 成功终态（独立事务）：result_ref 登记**产出对象**（资产版本号），
    #    资产侧另有 storage_ref.uri 指向生成产物本身。
    if await _transition_succeeded(
        sessionmaker, context, task_id, registration.asset_version_id
    ):
        stats.succeeded += 1
    else:
        stats.skipped += 1


async def _load(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
) -> GenerationTask | None:
    """按受信上下文重读任务快照（执行前的唯一事实来源）。"""
    async with sessionmaker() as session:
        await set_request_context(session, context.to_principal())
        return await SqlAlchemyGenerationTaskRepository(session).get_by_id(task_id)


def _format_reason(stage: str, exc: Exception) -> str:
    """把异常压成 audit_ref 可容纳的失败原因（分类标签 + 类型 + 摘要）。"""
    detail = f"{stage}:{type(exc).__name__}: {exc}"
    return detail[:_REASON_LIMIT]


async def _transition_failed(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
    reason: str,
) -> bool:
    """RUNNING→FAILED（终态，重试以新任务承载）；被栅栏挡下返回 False。"""
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        if task is None or task.status is not GenerationTaskStatus.RUNNING:
            return False
        try:
            await GenerationTaskLifecycle(repo).fail(
                task,
                reason=reason,
                actor=ACTOR,
                lease_owner=context.worker_id,
            )
            await session.commit()
            return True
        except DomainError as exc:
            if exc.error_code is ErrorRegistry.CONCURRENT_MODIFICATION:
                logger.warning(
                    "generation_finalize_fenced",
                    generation_task_id=task_id,
                    worker_id=context.worker_id,
                    outcome="failed",
                )
                return False
            raise


async def _transition_succeeded(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
    result_ref: str,
) -> bool:
    """RUNNING→SUCCEEDED（终态，登记产出对象引用）；被栅栏挡下返回 False。"""
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        if task is None or task.status is not GenerationTaskStatus.RUNNING:
            return False
        try:
            await GenerationTaskLifecycle(repo).succeed(
                task,
                result_ref=result_ref,
                actor=ACTOR,
                lease_owner=context.worker_id,
            )
            await session.commit()
            return True
        except DomainError as exc:
            if exc.error_code is ErrorRegistry.CONCURRENT_MODIFICATION:
                logger.warning(
                    "generation_finalize_fenced",
                    generation_task_id=task_id,
                    worker_id=context.worker_id,
                    outcome="succeeded",
                )
                return False
            raise


__all__ = [
    "BATCH_LIMIT",
    "GenerationExecutorContext",
    "GenerationRoundStats",
    "execute_round",
]
