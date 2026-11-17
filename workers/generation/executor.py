"""生成任务执行环：领取 → 适配器执行 → 产出登记 → 终态推进。

幂等与失败分类（对齐 delivery consumer 的纪律）：
- 领取（PENDING→RUNNING）以乐观版本控制裁决：``CONCURRENT_MODIFICATION``
  表示另一实例已领取 → 幂等跳过，不是失败；
- 适配器异常 → RUNNING→FAILED（终态），登记失败原因到 audit_ref；
- **产出登记失败同样进 FAILED**：登记不上资产 = 这次生成没有被交付的产物，
  记成 SUCCEEDED 会让"任务成功但没有东西"成为合法状态，主链会静静断开；
- FAILED 任务**绝不原地复活**：重试以新任务承载（attempt+1，9.4），
  数据库守卫触发器同表拦截直连 SQL 的复活尝试。

四段事务（ADR-R33）——顺序不能反：
1. 领取（generation 域，独立事务）；
2. 适配器执行（**事务外**：供应商调用不可持有数据库连接）；
3. 产出登记（**资产域**，资产域角色 + RLS，独立事务）；
4. 终态（generation 域，独立事务，登记产出对象引用）。

第 3 步必须在第 4 步之前：终态要写入产出对象引用（资产版本号），先终态后
登记会让"成功的任务指向一个不存在的资产"。若第 3 步成功而第 4 步失败，
下一轮重跑会重复登记——幂等由确定性资产版本号保证（见 asset_registrar）。

RLS 上下文：生成任务是项目级资源（restrictive project 策略），worker 用
受信编排上下文（``GenerationExecutorContext``）注入完整租户/组织/项目
范围，绝不从任务行自称范围（与 delivery consumer 同纪律）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.logging import get_logger
from packages.shared.rls import set_request_context
from services.generation_execution.domain.enums import GenerationTaskStatus
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

    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0  # 并发领取失败（另一实例抢先）：幂等跳过


async def execute_round(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    adapter: GenerationAdapter,
    stats: GenerationRoundStats,
    *,
    registrar: GenerationOutputRegistrar,
) -> GenerationRoundStats:
    """在单个受信上下文内执行一轮（领取至多 BATCH_LIMIT 个任务并推进终态）。

    每个任务一个独立事务：单个任务失败不影响同批其它任务的推进。

    ``registrar`` 刻意是关键字必填参数（不给默认值）：产出登记是主链闭环的
    组成部分，允许"没登记器也能跑"就等于允许一条静默降级的主链。
    """
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        pending_ids: list[str] = [
            task.entity_id
            for task in await SqlAlchemyGenerationTaskRepository(
                session
            ).list_pending(BATCH_LIMIT)
        ]
    for task_id in pending_ids:
        await _execute_one(sessionmaker, context, adapter, task_id, stats, registrar)
    return stats


async def _execute_one(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    adapter: GenerationAdapter,
    task_id: str,
    stats: GenerationRoundStats,
    registrar: GenerationOutputRegistrar,
) -> None:
    """执行单个任务：领取 → 执行 → 登记产出 → 终态（失败互不传染）。"""
    principal = context.to_principal()

    # 1) 领取（独立事务；并发领取由乐观版本控制裁决）。
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        lifecycle = GenerationTaskLifecycle(repo)
        task = await repo.get_by_id(task_id)
        if task is None:
            # 任务不可物理删除，PENDING 快照后消失只可能是防御分支。
            stats.skipped += 1
            return
        try:
            await lifecycle.claim(task, actor=ACTOR)
            await session.commit()
        except DomainError as exc:
            if exc.error_code is ErrorRegistry.CONCURRENT_MODIFICATION:
                stats.skipped += 1  # 另一实例已领取：幂等。
                return
            raise
    stats.claimed += 1

    # 2) 适配器执行在事务外：供应商调用（未来）不可持有数据库连接。
    try:
        output = await adapter.execute(task)
    except Exception as exc:
        reason = _format_reason("adapter", exc)
        await _transition_failed(sessionmaker, context, task_id, reason)
        stats.failed += 1
        logger.warning(
            "generation_task_failed",
            generation_task_id=task_id,
            stage="adapter",
            reason=reason,
        )
        return

    # 3) 产出登记（资产域事务）：登记不上就没有可交付的产物，进 FAILED。
    try:
        registration = await registrar.register(
            task=task, output=output, principal=principal
        )
    except Exception as exc:
        reason = _format_reason("output_registration", exc)
        await _transition_failed(sessionmaker, context, task_id, reason)
        stats.failed += 1
        logger.error(
            "generation_task_failed",
            generation_task_id=task_id,
            stage="output_registration",
            reason=reason,
            exc_info=True,
        )
        return

    # 4) 成功终态（独立事务）：result_ref 登记**产出对象**（资产版本号），
    #    资产侧另有 storage_ref.uri 指向生成产物本身。
    await _transition_succeeded(
        sessionmaker, context, task_id, registration.asset_version_id
    )
    stats.succeeded += 1


def _format_reason(stage: str, exc: Exception) -> str:
    """把异常压成 audit_ref 可容纳的失败原因（分类标签 + 类型 + 摘要）。"""
    detail = f"{stage}:{type(exc).__name__}: {exc}"
    return detail[:_REASON_LIMIT]


async def _transition_failed(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
    reason: str,
) -> None:
    """RUNNING→FAILED（终态，重试以新任务承载）。"""
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        if task is not None:
            await GenerationTaskLifecycle(repo).fail(
                task, reason=reason, actor=ACTOR
            )
            await session.commit()


async def _transition_succeeded(
    sessionmaker: SessionMaker,
    context: GenerationExecutorContext,
    task_id: str,
    result_ref: str,
) -> None:
    """RUNNING→SUCCEEDED（终态，登记产出对象引用）。"""
    principal = context.to_principal()
    async with sessionmaker() as session:
        await set_request_context(session, principal)
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        if task is not None and task.status is GenerationTaskStatus.RUNNING:
            await GenerationTaskLifecycle(repo).succeed(
                task, result_ref=result_ref, actor=ACTOR
            )
            await session.commit()


__all__ = [
    "BATCH_LIMIT",
    "GenerationExecutorContext",
    "GenerationRoundStats",
    "execute_round",
]
