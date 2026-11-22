"""Shot 应用服务（镜头状态推进与读取）。

创建不在本服务：Shot 的唯一诞生点是 ``SHOT.REVISION.CONFIRM``（ADR-R18 §12），
由 ``ShotDraftService.confirm_draft`` 在同一事务内原子创建 Shot + 首个 ShotRevision。
若这里再开一个 ``create_shot``，就会出现「绕过确认直接造镜头」的第二条路径。

守卫所需字段由本服务写入实体，再由领域层 ``transition_to`` 判定——
赋值与判定分离是刻意的：守卫必须能读到「这次请求打算写什么」，
但「能不能写」仍由领域层说了算。

另有一条**跨聚合**前置条件不归领域层：进入 IN_PRODUCTION 还要求父场次未归档
（SC-5 的并发侧）。它需要一次带行锁的读取，而领域层不做 IO；触发条件由边表
声明派生（``SHOT_IN_PRODUCTION_PARENT_PRECONDITION``），见 _assert_parent_accepts。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.lifecycle import ParentPrecondition
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import ReworkTaskStatus, ShotStatus
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.state_machine import (
    SHOT_LIFECYCLE,
    SHOT_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.shot_planning_repositories import (
    ReworkTaskRepository,
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.transition import advance_status


class ShotService:
    def __init__(
        self,
        repository: ShotRepository,
        rework_task_repository: ReworkTaskRepository,
        scene_repository: SceneRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._rework_task_repository = rework_task_repository
        self._scene_repository = scene_repository
        self._outbox_writer = outbox_writer

    async def get_shot(self, entity_id: str) -> Shot:
        shot = await self._repository.get_by_id(entity_id)
        if shot is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头不存在: {entity_id}")
        return shot

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: ShotStatus,
        changed_by: str,
        tenant_id: UUID,
        blocked_reason: str | None = None,
        rework_basis_ref: str | None = None,
        cancellation_ref: str | None = None,
        cancelled_at: datetime | None = None,
    ) -> Shot:
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )

        # fail-closed（contract_amendments.yaml V205-R14-A14 残留风险）：CANCELLED 的
        # 可解析授权记录对象仍未建模，cancellation_ref 只是调用方自报的字符串——
        # 领域守卫与库层 CHECK 都只能判「非空」，无法证明它对应一次真实生效的授权。
        # 按已登记裁定「运行时显式拒绝带 cancellation_ref 的推进」，在赋值前拒绝：
        # 既不把不可解析引用写进正式对象，也不让它经 authority_ref 进入审计事实。
        if cancellation_ref is not None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    "取消授权记录无法解析：CANCELLED 的可信授权对象尚未建模，"
                    "拒绝任何带 cancellation_ref 的推进"
                ),
            )

        shot = await self._repository.get_by_id(entity_id)
        if shot is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头不存在: {entity_id}")

        # 返工依据必须在库层可验证：引用一个不存在的 ReworkTask，等于用字符串
        # 绕过验收。这一步在赋值前做，避免把实体改成半合法状态再抛错。
        if rework_basis_ref is not None:
            task = await self._rework_task_repository.get_by_id(rework_basis_ref)
            if task is None:
                raise DomainError(
                    ErrorRegistry.NOT_FOUND,
                    detail=f"返工依据不存在: {rework_basis_ref}",
                )
            if task.status is ReworkTaskStatus.CLOSED:
                raise DomainError(
                    ErrorRegistry.STATE_TRANSITION_DENIED,
                    detail="已关闭的返工任务不能作为新的返工依据",
                )
            # 同项目归属：库层复合外键 (project_id, rework_basis_ref) 是最后防线，
            # 这里提前比对给出可读错误，避免让调用方收到裸的 23503。两层判据
            # 同一不变量（同项目），不是两套口径。
            if task.project_id != shot.project_id:
                raise DomainError(
                    ErrorRegistry.STATE_TRANSITION_DENIED,
                    detail=(
                        f"返工依据 {rework_basis_ref} 属于项目 {task.project_id}，"
                        f"与镜头所属项目 {shot.project_id} 不一致"
                    ),
                )

        # SC-5 的并发侧：进入在产前必须先与父场次的归档侧串行化。
        # 触发条件同样由边表派生（三条进入 IN_PRODUCTION 的边都声明了它），
        # 不是这里手写 ``if target is IN_PRODUCTION``。
        parent_precondition = SHOT_LIFECYCLE.parent_precondition(
            shot.status, target_status
        )
        if parent_precondition is not None:
            await self._assert_parent_accepts(shot, parent_precondition)

        shot.blocked_reason = blocked_reason
        shot.rework_basis_ref = rework_basis_ref
        shot.cancellation_ref = cancellation_ref
        shot.cancelled_at = cancelled_at

        # 授权依据写进迁移事实（D2：authority_ref 承载，不新建 ApprovalRecord 对象）。
        # cancellation_ref 已在入口 fail-closed 拒绝，此处只可能是 rework_basis_ref。
        authority_ref = rework_basis_ref

        await advance_status(
            shot,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="Shot",
            event_type="SHOT_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "shot_id": shot.entity_id,
                "project_id": shot.project_id,
                "scene_id": shot.scene_id,
            },
            resource_type="Shot",
            owner_action=SHOT_STATUS_CHANGE_ACTION,
            conflict_detail="镜头状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            authority_ref=authority_ref,
        )
        return shot

    async def _assert_parent_accepts(
        self, shot: Shot, precondition: ParentPrecondition
    ) -> None:
        """持共享行锁确认父场次未归档（SC-5 的并发侧，与归档侧的排他锁配对）。

        为什么子对象也要管这件事：只堵归档那一侧仍有一个窗口——归档事务已经锁住
        父行、判定「无在产镜头」、正在写状态；此刻另一个事务把镜头推入在产并提交，
        归档照样成功，库里就出现「已归档场次带着在产镜头」。

        共享锁的粒度是刻意的：它只与父行的**排他改写**互斥（归档），不与父行的
        普通更新互斥——为后者付串行化代价没有收益（锁模式成对的理由见
        repository 层的 ``scene_lock_statement``）。

        与领域守卫的分工：守卫判「这次请求带的字段够不够」，本函数判「另一张表里
        的行允许不允许」。前者一次查询即可，后者必须加锁——这正是父行锁不能塞进
        ``transition_to`` 的原因：领域层不做 IO。
        """
        parent = await self._scene_repository.lock_for_child_write(shot.scene_id)
        if parent is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"父场次不存在: {shot.scene_id}"
            )
        if parent.status.value == precondition.forbidden_status:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED, detail=precondition.message
            )
