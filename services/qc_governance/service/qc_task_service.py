"""QCTask 应用服务（QC 待检队列 + 租约，Phase 3）。

四条硬约束：
1. 入队幂等：同一 ``generation_task_id`` 只能对应一个 QC 任务（仓储唯一索引 +
   显式冲突检查 + 唯一冲突翻译），避免重复入队让同一产物被检两次；
2. 租约：领取/续租/判定/回收全部走本服务，状态事实与事件由唯一入口
   ``advance_status`` / ``save_versioned_entity`` 写入，没有第二处改状态的地方；
3. 判定必须有报告：``qc_report_id`` 为空直接拒绝——「无证据 PASS」是最危险的假绿。
   进一步，「有 ID」也不等于「有证据」：判定前必须核验该报告**真实存在**、
   且属于本任务引用的资产版本与同一租户（见 ``packages.shared.evidence_ports``）。
   跨域不建物理外键是架构选择，但不能因此把引用完整性降级为「字符串非空」。
4. 人工恢复不得凭自报：``recover`` 从数据库权威事实取「是否终态成功、携带哪个
   产物引用、归属哪个项目」，请求体只提供选择键（任务 + 项目 + 理由）。
   C7 的两条不变量（只在生成终态且携带已登记产物时入队；同一生成任务不得重复
   入队）在**同一条代码路径**上成立，人工与自动不可能各自漂移。

为什么人工与自动共用 ``enqueue`` 而不是各写一份
--------------------------------------------
「保留人工恢复能力，不是保留第二套 QC 任务创建机制」。两个入口共用一条创建
路径，幂等事实（唯一键）就只有一处可被破坏，来源差异只体现在审计载荷
（``EnqueueOrigin``）上。若分写两份，两条路径迟早对「终态」「产物引用」
给出不同判据，而它们的差异不会有人测到。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.evidence_ports import ReportLookup
from packages.shared.generation_ports import GenerationFacts
from packages.shared.port_errors import DuplicateEntityError
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    QCTaskStatus,
)
from services.project_planning.service.transition import (
    advance_status,
    save_versioned_entity,
)
from services.qc_governance.domain.qc_task import EnqueueOrigin, QCTask
from services.qc_governance.domain.state_machine import (
    QC_TASK_PROCESS_ACTION,
)


def _now() -> datetime:
    return datetime.now(UTC)


class QCTaskService:
    def __init__(
        self,
        repository: Any,
        outbox_writer: Any,
        *,
        report_lookup: ReportLookup | None = None,
    ) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer
        self._report_lookup = report_lookup

    async def enqueue(
        self,
        *,
        project_id: str,
        asset_version_id: str,
        generation_task_id: str,
        quality_profile_version: str,
        tenant_id: UUID,
        origin: EnqueueOrigin,
        shot_id: str = "",
        qc_task_id: str = "",
    ) -> QCTask:
        """创建 QUEUED。重复入队拒绝（业务冲突，不是 500）。

        ``origin`` 取代了原来的 ``created_by`` 字符串：操作者身份、来源、理由
        是同一组审计事实，拆成多个参数就会出现「subject_id 改了、载荷里还是旧值」
        这种只有审计时才被发现的漂移。

        幂等有三层，缺一不可：先查（便宜快路径）、唯一约束（并发兜底）、
        唯一冲突翻译（把 23505 变成可判定的 ``IDEMPOTENCY_CONFLICT``）。
        只有前两层时，并发失败会以 500 的形式出现，而不是「已入队」。
        """
        existing = await self._repository.find_by_generation_task(generation_task_id)
        if existing is not None:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"生成任务 {generation_task_id} 已入队（QC 任务 {existing.entity_id}）",
            )
        task = QCTask(
            entity_id=str(uuid4()),
            created_by=origin.operator_id,
            tenant_id=tenant_id,
            qc_task_id=qc_task_id,
            project_id=project_id,
            shot_id=shot_id,
            asset_version_id=asset_version_id,
            generation_task_id=generation_task_id,
            quality_profile_version=quality_profile_version,
        )
        try:
            await self._repository.add(task)
        except DuplicateEntityError as exc:
            # 并发窗口：先查都通过，唯一键只有一个能成立。这不是故障，是幂等生效。
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"生成任务 {generation_task_id} 已入队（并发冲突由唯一键兜底），"
                    "本条未创建新的 QC 任务"
                ),
            ) from exc
        self._outbox_writer.record(
            aggregate_type="QCTask",
            aggregate_id=task.entity_id,
            tenant_id=tenant_id,
            event_type="QC_TASK_QUEUED",
            payload={
                "qc_task_id": task.qc_task_id,
                "project_id": task.project_id,
                "asset_version_id": task.asset_version_id,
                "generation_task_id": task.generation_task_id,
                "quality_profile_version": task.quality_profile_version,
                "tenant_id": str(tenant_id),
                # 审计三元组：来源 + 操作者 + 理由。表里两条记录长得一样，
                # 只有载荷能区分「relay 投递」与「人工补录」。
                "source": origin.source,
                "operator_id": origin.operator_id,
                "reason": origin.reason,
            },
        )
        return task

    async def recover(
        self,
        *,
        facts: GenerationFacts,
        operator_id: str,
        reason: str,
    ) -> QCTask:
        """人工恢复入队（C7-b）：只做判定，事实由调用方从数据库读出。

        为什么事实是入参而不是在本方法里查
        ---------------------------------
        归属授权（``authorize_on_parent``）要判定「这个项目归不归你」，而它必须
        发生在**写入之前**；判定的对象又只能是数据库里的 project_id。若本方法
        自己再查一次事实，一次恢复就要读两遍生成任务，且两次读之间可以出现
        不一致（TOCTOU）。所以分工是：组合根读一次事实 → 用事实里的项目做授权
        → 把同一份事实交给这里判定。**不要为了「对称」把读取移进来**：那会让
        授权拿不到项目（``GenerationTask`` 的归属解析按 ``entity_id`` 走，
        业务 id 查不到），只能退化成「先写后判」。

        为什么不接收 ``asset_version_id`` / ``quality_profile_version``
        ----------------------------------------------------------
        这两个字段是 T3 从资产登记回执绑定的（ADR-R42 C1/C3）。让运维在请求体里
        填它们，等于重新开了一条「自报产物引用」的路——正是 C3 根治的问题。
        恢复的本质是「补一次漏掉的入队」，不是「重新定义这次生成了什么」。

        为什么终态但缺产物引用要拒绝而不是放行
        ------------------------------------
        那是 T3 绑定缺失的数据不一致。猜一个引用补上，会让一条「没有产物」的
        待检项通过 QC —— 比漏检更糟的假绿。
        """
        origin = EnqueueOrigin(source="MANUAL", operator_id=operator_id, reason=reason)
        if facts.status != GenerationTaskStatus.SUCCEEDED.value:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"生成任务 {facts.generation_task_id} 当前为 {facts.status}，"
                    "只有登记成功终态才可入队（C7：禁止绕开状态机）"
                ),
            )
        if not facts.asset_version_id or not facts.quality_profile_version:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"生成任务 {facts.generation_task_id} 为 {facts.status} 但缺少产物引用"
                    f"（asset_version_id={facts.asset_version_id!r}, "
                    f"quality_profile_version={facts.quality_profile_version!r}），"
                    "无法确定要检哪一份产物"
                ),
            )
        return await self.enqueue(
            project_id=facts.project_id,
            asset_version_id=facts.asset_version_id,
            generation_task_id=facts.generation_task_id,
            quality_profile_version=facts.quality_profile_version,
            tenant_id=facts.tenant_id,
            origin=origin,
            shot_id=facts.shot_id,
        )

    async def _load(self, task_id: str) -> QCTask:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"QC 任务不存在: {task_id}")
        return cast(QCTask, task)

    async def claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> QCTask:
        task = await self._load(task_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await advance_status(
            task,
            QCTaskStatus.RUNNING,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            event_type="QC_TASK_STARTED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count + 1,
            },
            resource_type="QCTask",
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 任务已被其他 Worker 领取，请勿重复领取",
            actor_field="worker_id",
            actor=worker_id,
            domain_operation=lambda audit_ref, action: task.claim(
                worker_id, expires, audit_ref
            ),
        )
        return task

    async def renew(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> QCTask:
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await save_versioned_entity(
            task,
            mutation=lambda audit_ref, action: task.renew(
                worker_id, expires, audit_ref
            ),
            audit_ref=f"qc-lease-renew:{uuid4()}",
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            aggregate_id=task.entity_id,
            event_type="QC_TASK_LEASE_RENEWED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count,
            },
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 租约续期并发冲突，请重试",
        )
        return task

    async def finish(
        self,
        *,
        task_id: str,
        worker_id: str,
        result: Literal["PASSED", "REJECTED"],
        qc_report_id: str,
        evidence_refs: list[str] | None = None,
    ) -> QCTask:
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        target = QCTaskStatus(result)
        await self._assert_report_backs_task(task, qc_report_id)
        payload = {
            "qc_task_id": task.qc_task_id,
            "worker_id": worker_id,
            "qc_report_id": qc_report_id,
            "evidence_refs": list(evidence_refs or []),
            "attempt_count": task.attempt_count,
        }
        # 两个分支各写字面量：事件契约靠源码字面量双向核对。
        if target is QCTaskStatus.PASSED:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="QCTask",
                event_type="QC_TASK_PASSED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="QCTask",
                owner_action=QC_TASK_PROCESS_ACTION,
                conflict_detail="QC 判定并发冲突，请重试",
                domain_operation=lambda audit_ref, action: task.finish(
                    worker_id, target, qc_report_id, audit_ref
                ),
            )
        else:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="QCTask",
                event_type="QC_TASK_REJECTED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="QCTask",
                owner_action=QC_TASK_PROCESS_ACTION,
                conflict_detail="QC 判定并发冲突，请重试",
                domain_operation=lambda audit_ref, action: task.finish(
                    worker_id, target, qc_report_id, audit_ref
                ),
            )
        return task

    async def _assert_report_backs_task(self, task: QCTask, qc_report_id: str) -> None:
        """判定前核验报告真实存在，且属于本任务的资产版本与租户。

        为什么不能只判非空
        ------------------
        ``qc_tasks.qc_report_id`` 是跨域引用、没有物理外键（两域分 schema）。
        如果这里只判 ``if not qc_report_id``，任何持有 ``QC.TASK.PROCESS`` 的
        调用方都能提交一个随便编的字符串把任务推成 PASSED——「有 ID」被当成
        「有证据」，而这正是 QC 存在的意义所在。

        为什么缺核验器要拒绝而不是放行
        ------------------------------
        未装配 ``report_lookup`` 说明调用方没打算验证据。放行等于让它成为默认
        路径；按 fail-closed 拒绝，装配错误会立刻暴露成一次显式失败，
        而不是悄悄退化成一串「看起来通过」的判定。
        """
        if self._report_lookup is None:
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=(
                    "QC 终结缺少报告核验器：判定必须有可核验的证据，"
                    "未装配 report_lookup 时拒绝终结（fail-closed）"
                ),
            )
        facts = await self._report_lookup(qc_report_id, task.tenant_id)
        if facts is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"QC 报告不存在: {qc_report_id}",
            )
        if facts.asset_version_id != task.asset_version_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"QC 报告 {qc_report_id} 属于资产版本 {facts.asset_version_id}，"
                    f"不是本任务引用的 {task.asset_version_id}"
                ),
            )
        if facts.tenant_id != task.tenant_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"QC 报告 {qc_report_id} 不属于本租户",
            )

    async def release_expired(self, *, task_id: str) -> QCTask:
        task = await self._load(task_id)
        await advance_status(
            task,
            QCTaskStatus.QUEUED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            event_type="QC_TASK_LEASE_EXPIRED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": task.lease_owner,
                "attempt_count": task.attempt_count,
            },
            resource_type="QCTask",
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 租约回收并发冲突，请重试",
            domain_operation=lambda audit_ref, action: task.release_expired(
                _now(), audit_ref
            ),
        )
        return task


__all__ = ["QCTaskService"]
