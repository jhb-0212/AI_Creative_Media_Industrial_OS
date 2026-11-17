"""ShotSpec 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现镜头规格用例：
  1. ``create_spec``：为已确认的正式 ShotRevision 建立结构化规格（8.12）；
  2. ``lock_spec``（SHOTSPEC.LOCK）：授权锁定维度，锁定维度/依据/版本和
     审批事件齐备。
- 契约口径：
  * ``create_spec`` 没有冻结授权动作（action_registry 无 SHOTSPEC.CREATE），
    属内部领域用例：Phase 1 主链中由镜头设计工作流（人工作业）调用，
    不暴露公共 HTTP 端点——授权动作是唯一的对外写入闸门，未登记动作
    不得私下开通道（fail-closed）。
  * ``lock_spec`` 是冻结 HIGH 风险动作（导演/授权 TD），授权由 API 层
    ``require_action + authorize`` 完成；服务层再做 Agent 防御性拒绝。
- 身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
- 抛出结构化 DomainError，不泄漏内部实现细节。
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.shot_spec import ShotSpec
from services.shot_planning.repository.shot_revision_repo import (
    ShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_repo import ShotSpecRepository


class ShotSpecService:
    """镜头规格创建与维度锁定用例。"""

    def __init__(
        self,
        spec_repository: ShotSpecRepository,
        revision_repository: ShotRevisionRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._spec_repository = spec_repository
        self._revision_repository = revision_repository
        self._outbox_writer = outbox_writer

    async def create_spec(
        self,
        *,
        shot_revision_id: str,
        duration_ms: int,
        aspect_ratio: str,
        fps: str,
        coordinate_system: str,
        camera_path: dict[str, Any],
        subject_paths: list[dict[str, Any]] | None = None,
        shot_size_changes: list[dict[str, Any]] | None = None,
        cut_points_frames: list[int] | None = None,
        previz_ref: str | None = None,
        control_signal_refs: list[str] | None = None,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ShotSpec:
        """为已确认的正式修订建立结构化规格（内部领域用例，8.12）。

        硬约束：
        - Agent 不得创建规格（C1：拆镜草案必须经人工确认才可进入 ShotSpec；
          Agent 只能产出 DRAFT/PROPOSAL，B2）；
        - 绑定的 ShotRevision 必须已存在（规格是修订的派生结构化描述，
          修订不存在即规格无落点）。
        """
        if principal.subject_type is SubjectType.AGENT:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得创建 ShotSpec（规格仅由授权人类工作流建立）",
            )

        shot_revision_id = shot_revision_id.strip()
        if not shot_revision_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_revision_id 不能为空"
            )

        # 绑定的修订必须已存在（同域内读取，不跨域）。
        revision = await self._revision_repository.get_by_id(shot_revision_id)
        if revision is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"绑定的镜头修订不存在: {shot_revision_id}",
            )

        spec = ShotSpec(
            entity_id=entity_id or uuid.uuid4().hex,
            shot_revision_id=shot_revision_id,
            shot_id=revision.shot_id,
            project_id=revision.project_id,
            duration_ms=duration_ms,
            aspect_ratio=aspect_ratio.strip(),
            fps=fps.strip(),
            coordinate_system=coordinate_system.strip(),
            camera_path=camera_path,
            subject_paths=subject_paths or [],
            shot_size_changes=shot_size_changes or [],
            cut_points_frames=cut_points_frames or [],
            previz_ref=previz_ref,
            control_signal_refs=control_signal_refs or [],
            created_by=principal.subject_id.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        try:
            await self._spec_repository.add(spec)
        except DuplicateEntityError as exc:
            existing = await self._spec_repository.get_by_id(spec.entity_id)
            if existing is not None:
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="镜头规格写入冲突"
            ) from exc

        # 说明：创建路径无冻结审计事件（action_registry 未登记 SHOTSPEC.CREATE），
        # 不发 outbox 事件；规格锁定（SHOTSPEC.LOCK）才是契约登记的审计事件源。
        return spec

    async def lock_spec(
        self,
        *,
        spec_id: str,
        project_id: str,
        dimensions: list[str],
        rationale: str,
        version: int,
        principal: Principal,
    ) -> ShotSpec:
        """授权锁定规格维度（SHOTSPEC.LOCK，HIGH，导演/授权 TD）。

        硬约束：
        - Agent 拒绝（防御性兜底；授权层已按 allowed_principals 拦截）；
        - 规格必须存在且归属请求项目（防跨项目锁定）；
        - 锁定必须携带依据（rationale），并以请求携带的 version 做乐观锁
          （「版本齐备」：并发修改返回 CONCURRENT_MODIFICATION）；
        - 锁定成功发出契约登记的审计事件 SHOTSPEC_LOCKED。
        """
        if principal.is_agent:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得锁定 ShotSpec（仅导演/授权 TD 可执行 SHOTSPEC.LOCK）",
            )

        spec_id = spec_id.strip()
        project_id = project_id.strip()
        if not spec_id:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="spec_id 不能为空")
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not rationale.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="锁定依据（rationale）不能为空"
            )

        spec = await self._spec_repository.get_by_id(spec_id)
        if spec is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"镜头规格不存在: {spec_id}"
            )
        if spec.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"规格 {spec_id} 归属项目 {spec.project_id} "
                    f"与请求 {project_id} 不一致"
                ),
            )

        expected_version = spec.entity_version
        if version != expected_version:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=(
                    f"规格 {spec_id} 当前版本为 {expected_version}，"
                    f"请求携带版本 {version}（锁定必须基于最新版本）"
                ),
            )

        spec.lock_dimensions(
            dimensions=dimensions,
            locked_by=principal.subject_id,
            rationale=rationale,
            audit_ref=f"SHOTSPEC.LOCK:{spec.entity_version}",
        )
        await self._spec_repository.save(spec, expected_version)

        self._outbox_writer.record(
            aggregate_type="ShotSpec",
            aggregate_id=spec.entity_id,
            event_type="SHOTSPEC_LOCKED",
            tenant_id=principal.tenant_id,
            payload={
                "shot_spec_id": spec.entity_id,
                "shot_revision_id": spec.shot_revision_id,
                "shot_id": spec.shot_id,
                "project_id": spec.project_id,
                "locked_dimensions": spec.locked_dimensions,
                "locked_by": spec.locked_by,
                "lock_rationale": spec.lock_rationale,
                "locked_version": spec.entity_version,
                "created_by": principal.subject_id,
                "tenant_id": str(principal.tenant_id),
                "status": spec.status.value,
            },
        )

        return spec
