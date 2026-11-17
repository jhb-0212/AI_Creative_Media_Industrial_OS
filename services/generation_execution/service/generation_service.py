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
  5. 路线必须是该镜头**当前生效路线**（ShotRouteSelection，V205-R14-A9）：
     未选路或路线不一致一律 409 ROUTE_NOT_EFFECTIVE。这是「单一路线生成」
     （Phase 1 验收 C4）在受理侧的强制——只守选路入口不守受理入口，
     同一镜头仍可在两个路线上各受理一次生成；
  6. 受理发出契约登记的审计事件 GENERATION_REQUESTED。
- ``rights_manifest_ref``（可选，ADR-R33）：本次产出的权利凭据引用。它不影响
  受理是否成立（生成可以先做），但决定产出能否进入批准链：资产批准要求
  权利证据非空，缺失时资产照样登记却不可批准——fail-closed，不伪造权利事实。
- 生命周期转移（claim / take_over / renew / succeed / fail）**只在**
  ``GenerationTaskLifecycle``：受理服务不暴露状态推进入口。保留两套推进路径
  等于保留一条绕过租约栅栏的写口（ADR-R34）。

**诚实边界（预算/配额数值校验）**：budget_reservation_ref /
quota_reservation_ref 的结构性准入（非空 + 引用归属）在这里强制；**数值级**
校验（不可超卖、租约有效期、余额扣减、BudgetLedger/QuotaReservation 状态
机推进）需要 budget_quota 域——该域当前为空壳（QuotaReservation 等 4 个
对象契约已冻结、运行时零承载）。伪造一个"预算通过"判定违反质量红线，
本服务不做，登记为显式债务（ADR-R32）：budget_quota 域实现后，把数值
校验插入本受理入口，引用面校验保持不变。
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    MAX_GENERATION_ATTEMPT,
    GenerationTask,
)
from services.generation_execution.domain.lease import lease_deadline
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
from services.generation_execution.repository.shot_route_selection_repo import (
    ShotRouteSelectionRepository,
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
        selection_repository: ShotRouteSelectionRepository,
        ref_ownership_port: RefOwnershipPort,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._task_repository = task_repository
        self._route_repository = route_repository
        self._selection_repository = selection_repository
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
        rights_manifest_ref: str | None = None,
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
            if attempt > MAX_GENERATION_ATTEMPT:
                # 9.4：重试"有上限"。没有这条，失败任务可被无限链式重放，
                # 成本与配额随之失控（每次重试都是一次真实生成）。
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"重试已达到上限（{MAX_GENERATION_ATTEMPT} 次）："
                        f"任务 {retry_of} 的 attempt 为 {retried.attempt}，"
                        "继续重试请走人工诊断（换参数/换路线须新建任务并说明原因）"
                    ),
                    context={
                        "retry_of": retry_of,
                        "attempt": retried.attempt,
                        "max_attempt": MAX_GENERATION_ATTEMPT,
                    },
                )

        # 3) 引用对象准入：存在、属于请求项目、**且绑定在请求镜头上**
        #    （跨域只读解析，ADR-R28 机制）。镜头维度不可省：只比项目归属时，
        #    同一项目内"镜头 A 的规格用于镜头 B"完全合法，产出与镜头意图错配。
        await self._assert_ref_owned(
            project_id=project_id,
            shot_id=shot_id,
            ref_field="shot_spec_ref",
            resource_type="ShotSpec",
            resource_id=shot_spec_ref.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        await self._assert_ref_owned(
            project_id=project_id,
            shot_id=shot_id,
            ref_field="visual_plan_ref",
            resource_type="ShotVisualPlan",
            resource_id=visual_plan_ref.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        if constraint_snapshot_ref != CONSTRAINT_NOT_APPLICABLE:
            await self._assert_ref_owned(
                project_id=project_id,
                shot_id=shot_id,
                ref_field="constraint_snapshot_ref",
                resource_type="ConstraintPack",
                resource_id=constraint_snapshot_ref,
                tenant_id=principal.tenant_id,
                organization_id=principal.organization_id,
            )

        # 4) 生效路线一致性（单一路线生成，Phase 1 验收 C4 / V205-R14-A9）：
        #    生成必须落在该镜头**当前生效**的路线上。
        #    没有这一条，「同一镜头在两个路线上各受理一次生成」仍然成立——
        #    路线的唯一性只在选路入口守着，受理入口却可以绕过它另开一条。
        #    顺序刻意排在最后：先判请求本身是否合法（对象存在 / 状态 / 归属），
        #    再判状态一致性——请求还没修好时告诉调用方"路线不是生效路线"
        #    只会把真正的输入错误藏起来。
        await self._assert_route_effective(
            project_id=project_id, shot_id=shot_id, route_id=route_id
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
            rights_manifest_ref=rights_manifest_ref,
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

    async def _assert_route_effective(
        self, *, project_id: str, shot_id: str, route_id: str
    ) -> None:
        """生成路线必须是该镜头当前生效路线（单一路线生成）。

        两种不成立的情形都给同一个结构化结论 ``ROUTE_NOT_EFFECTIVE``（409）：
        - 镜头尚未选路：拒绝而不是"顺手帮你选"——默认选路会让"谁定的路线"
          这个事实消失，出了问题无人可问；
        - 路线与生效路线不一致：拒绝而不是"按生效路线执行"——静默替换输入，
          调用方会以为自己在跑路线 B，产出的却是路线 A 的东西。

        用 409 而不是 422：请求本身是合法的，冲突来自平台当前状态。
        """
        effective = await self._selection_repository.get_effective(
            project_id=project_id, shot_id=shot_id
        )
        if effective is None:
            raise DomainError(
                ErrorRegistry.ROUTE_NOT_EFFECTIVE,
                detail=(
                    f"镜头 {project_id}/{shot_id} 尚无生效生产路线，"
                    "须先经 PRODUCTION.ROUTE.SELECT 选路（单一路线生成）"
                ),
                context={
                    "project_id": project_id,
                    "shot_id": shot_id,
                    "requested_route_id": route_id,
                },
            )
        if effective.route_id != route_id:
            raise DomainError(
                ErrorRegistry.ROUTE_NOT_EFFECTIVE,
                detail=(
                    f"路线 {route_id} 不是镜头 {project_id}/{shot_id} 的生效路线"
                    f"（生效路线为 {effective.route_id}）——单一路线生成拒绝"
                ),
                context={
                    "project_id": project_id,
                    "shot_id": shot_id,
                    "requested_route_id": route_id,
                    "effective_route_id": effective.route_id,
                    "effective_selection_id": effective.entity_id,
                },
            )

    async def _assert_ref_owned(
        self,
        *,
        project_id: str,
        shot_id: str,
        ref_field: str,
        resource_type: str,
        resource_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> None:
        """单条引用的 存在性(404) + 项目归属(422) + 镜头绑定(422) 校验。"""
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
        if ownership.shot_ids is not None and shot_id not in ownership.shot_ids:
            # 同项目跨镜头引用：不是权限问题，是"用错了对象"。
            # 单列一个错误文案（而不是并进上面的跨项目分支）：调用方需要知道
            # 该换引用还是该改 shot_id，两者处置完全不同。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"{ref_field} 指向的 {resource_type} 绑定在镜头 "
                    f"{sorted(ownership.shot_ids)} 上，与请求镜头 {shot_id} 不一致"
                    "（同项目跨镜头引用拒绝：产出会与镜头意图错配）"
                ),
                context={
                    "ref_field": ref_field,
                    "shot_id": shot_id,
                    "bound_shot_ids": sorted(ownership.shot_ids),
                },
            )
        if ownership.eligible is False:
            # 所有者域明确判定不可用（7.9：只有 ACTIVE 的约束包可作为生成输入）。
            # 消费域不解释对方状态词表，只认所有者给出的结论。
            reason = ownership.ineligible_reason or "所有者域判定该对象不可作为生成输入"
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{ref_field} 指向的 {resource_type} 不可用：{reason}",
            )

    # 状态推进不在这里：见 GenerationTaskLifecycle（ADR-R34 单一写路径）。


class GenerationTaskLifecycle:
    """worker 侧任务生命周期用例（接口隔离：只依赖任务仓储）。

    与 ``GenerationService``（受理用例）分离的原因：worker 执行环不需要
    路线仓储、引用归属端口和 outbox 写入端——把受理依赖强塞给 worker 会
    逼出 fail-closed 空实现（依赖即负担）。状态推进**只有这一条路径**。

    租约语义（ADR-R34，契约 9.4 / 12.6）：
    - ``claim``：PENDING→RUNNING，同时登记租约（谁在执行、租到什么时候）；
    - ``take_over``：**状态不变**的接管——只允许对「租约已过期」的任务做，
      原子谓词 CAS 在仓储层；接管不是状态转移，所以状态机不该为此新增边
      （租约与状态是正交事实，硬塞进状态机会把「租期」说成「任务阶段」）；
    - ``renew``：持有者续租；长耗时执行（真实供应商调用）必须周期性续租，
      否则会被当成崩溃接管（Phase 1 确定性适配器亚秒级，执行环未接续租，
      债务见 ADR-R34 §4）；
    - ``succeed`` / ``fail``：终态写入前校验租约持有者。这是**栅栏**：
      被接管的僵尸 worker 即使还活着，也不能把终态写回它那一版结果——
      否则"旧执行的结果覆盖新执行"会以看似合法的状态转移发生。
    """

    def __init__(self, task_repository: GenerationTaskRepository) -> None:
        self._task_repository = task_repository

    async def claim(
        self,
        task: GenerationTask,
        *,
        actor: str,
        lease_owner: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> None:
        """领取任务（PENDING→RUNNING，登记租约；并发领取由乐观锁裁决）。"""
        claimed_at = now or datetime.now(UTC)
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.RUNNING)
        task.status = GenerationTaskStatus.RUNNING
        task.lease_owner = lease_owner
        task.lease_expires_at = lease_deadline(lease_seconds, claimed_at)
        task.started_at = claimed_at
        task._advance_version(audit_ref=f"claim:{actor}@{lease_owner}")
        await self._task_repository.save(task, expected_version=expected)

    async def take_over(
        self,
        task: GenerationTask,
        *,
        actor: str,
        lease_owner: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> None:
        """接管过期租约（状态保持 RUNNING；CAS 未命中即并发冲突）。

        ``started_at`` 刻意不改写：首次开始执行是既成事实；接管次数若需要
        度量化，应从审计事件统计，而不是覆盖时间戳。

        本方法**不改写传入的任务快照**：快照是夺取前的状态，改写它会让调用方
        误以为"本地对象即事实"。实际归属以数据库行为准，调用方必须重读。
        """
        taken_at = now or datetime.now(UTC)
        if task.status is not GenerationTaskStatus.RUNNING:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=(
                    f"任务 {task.entity_id} 状态为 {task.status.value}，"
                    "只有 RUNNING 任务存在可接管的租约"
                ),
            )
        taken = await self._task_repository.take_over(
            task.entity_id,
            worker_id=lease_owner,
            expires_at=lease_deadline(lease_seconds, taken_at),
            now=taken_at,
        )
        if not taken:
            # 租约未到期，或另一个 worker 抢先接管：都是幂等跳过，不是失败。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=(
                    f"任务 {task.entity_id} 的租约未被 {lease_owner} 接管"
                    "（未到期或已被并发接管）"
                ),
            )

    async def renew(
        self,
        task: GenerationTask,
        *,
        lease_owner: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> None:
        """持有者续租（延长 ``lease_expires_at``；非持有者一律拒绝）。"""
        renewed_at = now or datetime.now(UTC)
        self._assert_holder(task, lease_owner)
        expected = task.entity_version
        task.lease_expires_at = lease_deadline(lease_seconds, renewed_at)
        task._advance_version(audit_ref=f"renew:{lease_owner}")
        await self._task_repository.save(task, expected_version=expected)

    async def succeed(
        self,
        task: GenerationTask,
        *,
        result_ref: str,
        actor: str,
        lease_owner: str,
        now: datetime | None = None,
    ) -> None:
        """执行成功（RUNNING→SUCCEEDED，登记产出引用；须持有租约）。"""
        completed_at = now or datetime.now(UTC)
        self._assert_holder(task, lease_owner)
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.SUCCEEDED)
        task.status = GenerationTaskStatus.SUCCEEDED
        task.result_ref = result_ref
        task.completed_at = completed_at
        task._advance_version(audit_ref=f"succeed:{actor}@{lease_owner}")
        await self._task_repository.save(task, expected_version=expected)

    async def fail(
        self,
        task: GenerationTask,
        *,
        reason: str,
        actor: str,
        lease_owner: str,
        now: datetime | None = None,
    ) -> None:
        """执行失败（RUNNING→FAILED，终态；重试以新任务承载）。"""
        completed_at = now or datetime.now(UTC)
        self._assert_holder(task, lease_owner)
        expected = task.entity_version
        assert_transition_generation_task(task.status, GenerationTaskStatus.FAILED)
        task.status = GenerationTaskStatus.FAILED
        task.completed_at = completed_at
        task._advance_version(audit_ref=f"fail:{actor}@{lease_owner}:{reason[:64]}")
        await self._task_repository.save(task, expected_version=expected)

    @staticmethod
    def _assert_holder(task: GenerationTask, lease_owner: str) -> None:
        """栅栏：只有当前租约持有者能推进终态。"""
        if task.lease_owner != lease_owner:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=(
                    f"任务 {task.entity_id} 的租约持有者为 "
                    f"{task.lease_owner!r}，拒绝 {lease_owner!r} 写入终态"
                    "（租约已被接管，ADR-R34 栅栏）"
                ),
            )
