"""ShotVisualPlan 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现镜头视觉方案生命周期用例：
  1. ``create_plan``：建立视觉方案草案（锁定/开放字段分离，19.2）；
  2. ``submit_plan`` / ``approve_plan`` / ``reject_plan``：送审与审阅结论；
  3. ``lock_plan_fields``：逐字段授权锁定（带锁定人/时间/理由/版本）。
- 契约口径：
  * 这些用例没有冻结授权动作（action_registry 未登记 SHOTVISUALPLAN.*），
    属内部领域用例：Phase 1 主链中由镜头设计工作流（人工作业）调用，
    不暴露公共 HTTP 端点——授权动作是唯一的对外写入闸门，未登记动作
    不得私下开通道（fail-closed）；
  * 职责分离（B4/C.5.1）：审阅人不得与创建人同一主体；
  * AI 不得静默修改锁定字段（19.2）：Agent 只能创建 DRAFT，不得推进
    审阅/锁定路径。
- 身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal, SubjectType
from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan
from services.shot_planning.repository.shot_visual_plan_repo import (
    ShotVisualPlanRepository,
)


class ShotVisualPlanService:
    """镜头视觉方案生命周期用例。"""

    def __init__(self, plan_repository: ShotVisualPlanRepository) -> None:
        self._plan_repository = plan_repository

    async def create_plan(
        self,
        *,
        shot_id: str,
        project_id: str,
        intent_summary: str,
        camera_language: str,
        shot_size: str,
        lens_range: str,
        motion_intent: str,
        lighting_intent: str,
        open_fields: list[str],
        locked_fields: list[dict[str, Any]] | None = None,
        revision: int = 1,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ShotVisualPlan:
        """建立视觉方案草案（内部领域用例，19.2）。

        硬约束：Agent 只能产出 DRAFT（B2）——创建即 DRAFT，且 Agent 创建
        合法（方案草案是 Agent 的创意产出物）；审阅/锁定只能由人类完成。
        """
        shot_id = shot_id.strip()
        project_id = project_id.strip()
        if not shot_id:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="shot_id 不能为空")
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        open_fields = [f.strip() for f in open_fields if f.strip()]
        if not open_fields:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="open_fields 不能为空（锁定与开放字段分离，19.2）",
            )

        plan = ShotVisualPlan(
            entity_id=entity_id or uuid.uuid4().hex,
            shot_id=shot_id,
            project_id=project_id,
            intent_summary=intent_summary.strip(),
            camera_language=camera_language.strip(),
            shot_size=shot_size.strip(),
            lens_range=lens_range.strip(),
            motion_intent=motion_intent.strip(),
            lighting_intent=lighting_intent.strip(),
            locked_fields=locked_fields or [],
            open_fields=open_fields,
            revision=revision,
            created_by=principal.subject_id.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        try:
            await self._plan_repository.add(plan)
        except DuplicateEntityError as exc:
            existing = await self._plan_repository.get_by_id(plan.entity_id)
            if existing is not None:
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="视觉方案写入冲突"
            ) from exc

        return plan

    async def submit_plan(
        self, *, plan_id: str, principal: Principal
    ) -> ShotVisualPlan:
        """送审（DRAFT -> REVIEW）。Agent 不得推进审阅路径。"""
        plan = await self._require_human_editable_plan(plan_id, principal)
        expected_version = plan.entity_version
        plan.submit(audit_ref=f"SUBMIT:{plan.entity_version}")
        await self._plan_repository.save(plan, expected_version)
        return plan

    async def approve_plan(
        self, *, plan_id: str, principal: Principal
    ) -> ShotVisualPlan:
        """审阅通过（REVIEW -> APPROVED）。

        职责分离（B4）：审阅人不得与创建人同一主体。
        """
        plan = await self._require_human_editable_plan(plan_id, principal)
        if plan.created_by == principal.subject_id:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="职责分离：视觉方案的审阅人不得与创建人同一主体（C.5.1）",
            )
        expected_version = plan.entity_version
        plan.approve(audit_ref=f"APPROVE:{plan.entity_version}")
        await self._plan_repository.save(plan, expected_version)
        return plan

    async def reject_plan(
        self, *, plan_id: str, principal: Principal
    ) -> ShotVisualPlan:
        """审阅拒绝（REVIEW -> REJECTED，终态；变更须新建 revision）。"""
        plan = await self._require_human_editable_plan(plan_id, principal)
        if plan.created_by == principal.subject_id:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="职责分离：视觉方案的审阅人不得与创建人同一主体（C.5.1）",
            )
        expected_version = plan.entity_version
        plan.reject(audit_ref=f"REJECT:{plan.entity_version}")
        await self._plan_repository.save(plan, expected_version)
        return plan

    async def lock_plan_fields(
        self,
        *,
        plan_id: str,
        field_names: list[str],
        reason: str,
        principal: Principal,
    ) -> ShotVisualPlan:
        """逐字段授权锁定（19.2：锁定字段带锁定人/时间/理由/版本）。"""
        plan = await self._require_human_editable_plan(plan_id, principal)
        expected_version = plan.entity_version
        plan.lock_fields(
            field_names=field_names,
            locked_by=principal.subject_id,
            reason=reason,
            audit_ref=f"LOCK_FIELDS:{plan.entity_version}",
        )
        await self._plan_repository.save(plan, expected_version)
        return plan

    async def _require_human_editable_plan(
        self, plan_id: str, principal: Principal
    ) -> ShotVisualPlan:
        """公共前置校验：方案存在 + 主体为人类（Agent 不得推进生命周期）。"""
        if principal.subject_type is SubjectType.AGENT:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得推进视觉方案生命周期（仅授权人类可执行）",
            )
        plan_id = plan_id.strip()
        if not plan_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="plan_id 不能为空"
            )
        plan = await self._plan_repository.get_by_id(plan_id)
        if plan is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"视觉方案不存在: {plan_id}"
            )
        return plan
