"""GenerationTask 应用服务（GENERATION.REQUEST 受理 + 任务生命周期用例）。

职责：
- ``request_generation``（GENERATION.REQUEST，MEDIUM，导演/制片/授权范围内
  Agent）：生成任务受理。准入校验（动作矩阵 approval_requirement
  "预算、配额、依赖与约束快照通过"的当前可强制实现面）：
  1. 路线存在且 VERIFIED（404 / ACT-ROUTE-NEG-001 同源拒绝）；
  2. shot_spec_ref / visual_plan_ref（及非哨兵的 constraint_snapshot_ref）
     存在且属于请求声明的项目——经 RefOwnershipPort 跨域只读解析
     （ADR-R28 机制复用：读经过所有者域自己的数据库身份）。幽灵引用 404、
     跨项目引用 422（ACT-GEN-NEG-001 权限越界拒绝的引用面）；
  3. constraint_snapshot_ref 接受 NOT_APPLICABLE 哨兵（31.3：无锁定约束
     镜头标记 NOT_APPLICABLE）；
  4. retry_of（可选）必须指向同项目同镜头的 FAILED 终态任务，新任务
     attempt = 被重试任务 attempt + 1——重试以新任务承载，不在原任务复活；
  5. 受理发出契约登记的审计事件 GENERATION_REQUESTED。
- ``claim`` / ``succeed`` / ``fail``：worker 生命周期转移（乐观并发控制）。

**诚实边界（预算/配额数值校验）**：budget_reservation_ref /
quota_reservation_ref 的结构性准入（非空 + 引用归属）在这里强制；**数值级**
校验（不可超卖、租约有效期、余额扣减、BudgetLedger/QuotaReservation 状态
机推进）需要 budget_quota 域——该域当前为空壳（QuotaReservation 等 4 个
对象契约已冻结、运行时零承载）。伪造一个"预算通过"判定违反质量红线，
本服务不做，登记为显式债务（ADR-R32）：budget_quota 域实现后，把数值
校验插入本受理入口，引用面校验保持不变。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)
from services.generation_execution.domain.generation_task import (
    CONSTRAINT_NOT_APPLICABLE,
    GenerationTask,
)
from services.generation_execution.domain.ports import RefOwnershipPort
from services.generation_execution.domain.state_machine import (
    assert_transition_generation_task,
)
from services.generation_execution.repository.generation_task_repo import (
    GenerationTaskRepository,
)
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)

#: 引用字段 →（解析资源类型）。constraint_snapshot_ref 的哨兵值单独处理。
_REF_RESOURCE_TYPES: tuple[tuple[str, str], ...] = (
    ("shot_spec_ref", "ShotSpec"),
    ("visual_plan_ref", "ShotVisualPlan"),
)


class GenerationService:
    """生成任务受理与生命周期用例。"""

    def __init__(
        self,
        task_repository: GenerationTaskRepository,
        route_repository: ProductionRouteRepository,
        ref_ownership_port: RefOwnershipPort,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._task_repository = task_repository
        self._route_repository = route_repository
        self._ref_ownership_port = ref_ownership_port
        self._outbox_writer = outbox_writer

    # ------------------------------------------------------------------
    # GENERATION.REQUEST（受理）
    # ------------------------------------------------------------------

    async def request_generation(
        self,
        *,
        project_id: str,
        shot_id: str,
        route_id: str,
        shot_spec_ref: str,
        visual_plan_ref: str,
        constraint_snapshot_ref: str,
        budget_reservation_ref: str,
        quota_reservation_ref: str,
        dependency_refs: list[str],
        principal: Principal,
        retry_of: str | None = None,
    ) -> GenerationTask:
        """受理生成任务（GENERATION.REQUEST，准入校验全过才落 PENDING）。"""
        project_id = project_id.strip()
        shot_id = shot_id.strip()
        route_id = route_id.strip()
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not shot_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_id 不能为空"
            )
        if not route_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="route_id 不能为空"
            )
        if not budget_reservation_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="budget_reservation_ref 不能为空（预算准入，ACT-GEN-NEG-001）",
            )
        if not quota_reservation_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="quota_reservation_ref 不能为空（配额准入，ACT-GEN-NEG-001）",
            )
        constraint_snapshot_ref = constraint_snapshot_ref.strip()
        if not constraint_snapshot_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="constraint_snapshot_ref 不能为空（或显式 NOT_APPLICABLE）",
            )

        # 1) 路线准入：存在 + VERIFIED（与 PRODUCTION.ROUTE.SELECT 同源语义）。
        route = await self._route_repository.get_by_id(route_id)
        if route is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"路线不存在: {route_id}"
            )
        if route.status is not ProductionRouteStatus.VERIFIED:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"路线 {route_id} 状态为 {route.status.value}，"
                    "未验证路线不得进入生成（须处于 VERIFIED）"
                ),
            )

        # 2) 重试链准入：retry_of 必须指向同项目同镜头的 FAILED 终态任务。
        attempt = 1
        if retry_of is not None:
            retried = await self._task_repository.get_by_id(retry_of.strip())
            if retried is None:
                raise DomainError(
                    ErrorRegistry.NOT_FOUND, detail=f"被重试任务不存在: {retry_of}"
                )
            if retried.status is not GenerationTaskStatus.FAILED:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"被重试任务 {retry_of} 状态为 {retried.status.value}，"
                        "只有 FAILED 终态任务可被重试（重试以新任务承载，9.4）"
                    ),
                )
            if retried.project_id != project_id or retried.shot_id != shot_id:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="被重试任务与请求的项目/镜头不一致",
                )
            attempt = retried.attempt + 1

        # 3) 引用对象准入：存在且属于请求项目（跨域只读解析，ADR-R28 机制）。
        await self._assert_ref_owned(
            project_id=project_id,
            ref_field="shot_spec_ref",
            resource_type="ShotSpec",
            resource_id=shot_spec_ref.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        await self._assert_ref_owned(
            project_id=project_id,
            ref_field="visual_plan_ref",
            resource_type="ShotVisualPlan",
            resource_id=visual_plan_ref.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        if constraint_snapshot_ref != CONSTRAINT_NOT_APPLICABLE:
            await self._assert_ref_owned(
                project_id=project_id,
                ref_field="constraint_snapshot_ref",
                resource_type="ConstraintPack",
                resource_id=constraint_snapshot_ref,
                tenant_id=principal.tenant_id,
                organization_id=principal.organization_id,
            )

        task = GenerationTask(
            entity_id=f"gen_{uuid4().hex[:12]}",
            created_by=principal.subject_id,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            project_id=project_id,
            shot_id=shot_id,
            route_id=route_id,
            shot_spec_ref=shot_spec_ref.strip(),
            visual_plan_ref=visual_plan_ref.strip(),
            constraint_snapshot_ref=constraint_snapshot_ref,
            budget_reservation_ref=budget_reservation_ref.strip(),
            quota_reservation_ref=quota_reservation_ref.strip(),
            dependency_refs=list(dependency_refs),
            attempt=attempt,
            retry_of=retry_of.strip() if retry_of else None,
            status=GenerationTaskStatus.PENDING,
        )
        await self._task_repository.add(task)

        self._outbox_writer.record(
            aggregate_type="GenerationTask",
            aggregate_id=task.entity_id,
            event_type="GENERATION_REQUESTED",
            tenant_id=principal.tenant_id,
            payload={
                "generation_task_id": task.entity_id,
                "project_id": task.project_id,
                "shot_id": task.shot_id,
                "route_id": task.route_id,
                "attempt": task.attempt,
                "retry_of": task.retry_of,
                "requested_by": principal.subject_id,
                "tenant_id": str(principal.tenant_id),
                "status": task.status.value,
            },
        )
        return task

    async def _assert_ref_owned(
        self,
        *,
        project_id: str,
        ref_field: str,
        resource_type: str,
        resource_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> None:
        """单条引用的 存在性(404) + 归属(422) 校验。"""
        if not resource_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail=f"{ref_field} 不能为空"
            )
        if not self._ref_ownership_port.can_resolve(resource_type):
            # 解析器缺失是组合根装配缺陷，不是请求错误。
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"引用类型 {resource_type} 无归属解析器（组合根装配缺失）",
            )
        ownership = await self._ref_ownership_port.resolve(
            tenant_id=tenant_id,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if ownership is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"{ref_field} 指向的 {resource_type} 不存在: {resource_id}",
            )
        if ownership.project_id != project_id:
            # ACT-GEN-NEG-001：跨项目引用 = 权限越界拒绝。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"{ref_field} 指向的 {resource_type} 不属于项目 {project_id}"
                    "（跨项目引用拒绝，ACT-GEN-NEG-001）"
                ),
            )

    # ------------------------------------------------------------------
    # worker 生命周期（PENDING→RUNNING→SUCCEEDED/FAILED）
    # ------------------------------------------------------------------

    async def claim(self, task: GenerationTask, *, principal: Principal) -> None:
        """领取任务（PENDING→RUNNING；并发领取由乐观版本控制裁决）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.RUNNING)
        task.status = GenerationTaskStatus.RUNNING
        task._advance_version(audit_ref=f"claim:{principal.subject_id}")
        await self._task_repository.save(task, expected_version=expected)

    async def succeed(
        self, task: GenerationTask, *, result_ref: str, principal: Principal
    ) -> None:
        """执行成功（RUNNING→SUCCEEDED，登记产出引用）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.SUCCEEDED)
        task.status = GenerationTaskStatus.SUCCEEDED
        task.result_ref = result_ref
        task._advance_version(audit_ref=f"succeed:{principal.subject_id}")
        await self._task_repository.save(task, expected_version=expected)

    async def fail(
        self, task: GenerationTask, *, reason: str, principal: Principal
    ) -> None:
        """执行失败（RUNNING→FAILED，终态；重试以新任务承载）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.FAILED)
        task.status = GenerationTaskStatus.FAILED
        task._advance_version(audit_ref=f"fail:{principal.subject_id}:{reason[:64]}")
        await self._task_repository.save(task, expected_version=expected)


class GenerationTaskLifecycle:
    """worker 侧任务生命周期用例（接口隔离：只依赖任务仓储）。

    与 ``GenerationService``（受理用例）分离的原因：worker 执行环不需要
    路线仓储、引用归属端口和 outbox 写入端——把受理依赖强塞给 worker 会
    逼出 fail-closed 空实现（依赖即负担）。生命周期只做三件事：领取、
    成功终态、失败终态，全部经 ``assert_transition_generation_task`` 校验。
    """

    def __init__(self, task_repository: GenerationTaskRepository) -> None:
        self._task_repository = task_repository

    async def claim(self, task: GenerationTask, *, actor: str) -> None:
        """领取任务（PENDING→RUNNING；并发领取由乐观版本控制裁决）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.RUNNING)
        task.status = GenerationTaskStatus.RUNNING
        task._advance_version(audit_ref=f"claim:{actor}")
        await self._task_repository.save(task, expected_version=expected)

    async def succeed(
        self, task: GenerationTask, *, result_ref: str, actor: str
    ) -> None:
        """执行成功（RUNNING→SUCCEEDED，登记产出引用）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.SUCCEEDED)
        task.status = GenerationTaskStatus.SUCCEEDED
        task.result_ref = result_ref
        task._advance_version(audit_ref=f"succeed:{actor}")
        await self._task_repository.save(task, expected_version=expected)

    async def fail(self, task: GenerationTask, *, reason: str, actor: str) -> None:
        """执行失败（RUNNING→FAILED，终态；重试以新任务承载）。"""
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.FAILED)
        task.status = GenerationTaskStatus.FAILED
        task._advance_version(audit_ref=f"fail:{actor}:{reason[:64]}")
        await self._task_repository.save(task, expected_version=expected)
