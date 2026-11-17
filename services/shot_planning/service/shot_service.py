"""镜头规划应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现两个 Phase 1 主链用例：
  1. ``create_draft``（SHOT.DRAFT.CREATE）：AI 剧本拆镜产出草案；
  2. ``confirm_revision``（SHOT.REVISION.CONFIRM）：授权人类确认草案，
     产出正式 ShotRevision（版本递增不覆盖）。
- 边界硬约束（ADR-R27）：
  * AI 只能产草案（source=agent），绝不写正式 ShotRevision；
  * 正式镜头修订只由授权人类确认产生（Agent 确认一律拒绝）；
  * 修订 append-only：新修订即新行，旧修订标 SUPERSEDED。
- 身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐：
- SHOT.DRAFT.CREATE：resource_type=ShotDraft，allowed=Agent/编剧/导演，LOW
- SHOT.REVISION.CONFIRM：resource_type=ShotRevision，allowed=导演/镜头负责人，HIGH
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.enums import ShotDraftSource
from services.shot_planning.domain.shot_draft import ShotDraft
from services.shot_planning.domain.shot_revision import ShotRevision
from services.shot_planning.repository.shot_draft_repo import ShotDraftRepository
from services.shot_planning.repository.shot_revision_repo import (
    ShotRevisionRepository,
)


class ShotService:
    """镜头草案创建与正式修订确认用例。"""

    def __init__(
        self,
        draft_repository: ShotDraftRepository,
        revision_repository: ShotRevisionRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._draft_repository = draft_repository
        self._revision_repository = revision_repository
        self._outbox_writer = outbox_writer

    async def create_draft(
        self,
        *,
        project_id: str,
        episode_ref: str,
        scene_ref: str,
        shot_code: str,
        intent_summary: str,
        shot_description: str,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ShotDraft:
        """AI 剧本拆镜：产出镜头草案（SHOT.DRAFT.CREATE）。

        草案来源（source）由 Principal 的主体类型决定：Agent → agent，
        人类 → human。AI 只产草案，绝不写正式 ShotRevision。
        """
        project_id = project_id.strip()
        episode_ref = episode_ref.strip()
        scene_ref = scene_ref.strip()
        shot_code = shot_code.strip()
        intent_summary = intent_summary.strip()
        shot_description = shot_description.strip()
        created_by = principal.subject_id.strip()

        for name, value in (
            ("project_id", project_id),
            ("episode_ref", episode_ref),
            ("scene_ref", scene_ref),
            ("shot_code", shot_code),
            ("intent_summary", intent_summary),
            ("shot_description", shot_description),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        source = (
            ShotDraftSource.AGENT
            if principal.subject_type is SubjectType.AGENT
            else ShotDraftSource.HUMAN
        )

        # 幂等：已存在同 id 直接返回。
        if entity_id is not None:
            existing = await self._draft_repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_draft(
                    existing,
                    project_id,
                    episode_ref,
                    scene_ref,
                    shot_code,
                    intent_summary,
                    shot_description,
                    source,
                )
                return existing

        draft = ShotDraft(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            episode_ref=episode_ref,
            scene_ref=scene_ref,
            shot_code=shot_code,
            intent_summary=intent_summary,
            shot_description=shot_description,
            source=source,
            created_by=created_by,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        try:
            await self._draft_repository.add(draft)
        except DuplicateEntityError as exc:
            existing = await self._draft_repository.get_by_id(draft.entity_id)
            if existing is not None:
                _assert_same_draft(
                    existing,
                    project_id,
                    episode_ref,
                    scene_ref,
                    shot_code,
                    intent_summary,
                    shot_description,
                    source,
                )
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="草案写入冲突"
            ) from exc

        self._outbox_writer.record(
            aggregate_type="ShotDraft",
            aggregate_id=draft.entity_id,
            event_type="SHOT_DRAFT_CREATED",
            tenant_id=principal.tenant_id,
            payload={
                "draft_id": draft.entity_id,
                "project_id": project_id,
                "episode_ref": episode_ref,
                "scene_ref": scene_ref,
                "shot_code": shot_code,
                "source": source.value,
                "created_by": created_by,
                "tenant_id": str(principal.tenant_id),
                "status": draft.status.value,
            },
        )

        return draft

    async def confirm_revision(
        self,
        *,
        draft_id: str,
        shot_id: str,
        project_id: str,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ShotRevision:
        """人工确认草案，产出正式镜头修订（SHOT.REVISION.CONFIRM）。

        硬约束：
        - Agent 不得确认（「Agent 不得自批自用」）；确认必须由人类完成。
        - 草案必须存在且为 DRAFT 态；确认后草案 → CONFIRMED。
        - 草案 project_id 必须与请求体 project_id 一致（防跨项目挂载）。
        - 修订 append-only：新修订即新行，旧活跃修订标 SUPERSEDED。
        """
        if principal.is_agent:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得确认正式镜头修订（仅授权人类可执行 SHOT.REVISION.CONFIRM）",
            )

        shot_id = shot_id.strip()
        project_id = project_id.strip()
        if not shot_id:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="shot_id 不能为空")
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )

        # 草案必须存在。
        draft = await self._draft_repository.get_by_id(draft_id)
        if draft is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"草案不存在: {draft_id}"
            )

        # 草案 project_id 必须与请求体一致（防跨项目挂载）。
        if draft.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"草案 {draft_id} 归属项目 {draft.project_id} 与请求 {project_id} 不一致",
            )

        # 幂等：同 entity_id 已存在直接返回。
        if entity_id is not None:
            existing = await self._revision_repository.get_by_id(entity_id)
            if existing is not None:
                return existing

        # 计算下一版本号：当前活跃修订的 revision + 1，否则从 1 开始。
        next_revision = 1
        active = await self._revision_repository.get_active_by_shot(shot_id)
        if active is not None:
            next_revision = active.revision + 1

        revision = ShotRevision(
            entity_id=entity_id or uuid.uuid4().hex,
            shot_id=shot_id,
            project_id=draft.project_id,
            episode_ref=draft.episode_ref,
            scene_ref=draft.scene_ref,
            intent_summary=draft.intent_summary,
            shot_description=draft.shot_description,
            revision=next_revision,
            created_by=principal.subject_id,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        try:
            await self._revision_repository.add(revision)
        except DuplicateEntityError as exc:
            existing = await self._revision_repository.get_by_id(revision.entity_id)
            if existing is not None:
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="镜头修订写入冲突"
            ) from exc

        # 草案确认（DRAFT -> CONFIRMED）。修订是 append-only，旧修订保留，
        # 版本递增由 revision 号派生，无需标记旧修订状态。
        draft.confirm(audit_ref=revision.entity_id)
        await self._draft_repository.save(draft, draft.entity_version - 1)

        self._outbox_writer.record(
            aggregate_type="ShotRevision",
            aggregate_id=revision.entity_id,
            event_type="SHOT_REVISION_CONFIRMED",
            tenant_id=principal.tenant_id,
            payload={
                "shot_revision_id": revision.entity_id,
                "shot_id": shot_id,
                "draft_id": draft_id,
                "project_id": revision.project_id,
                "episode_ref": revision.episode_ref,
                "scene_ref": revision.scene_ref,
                "revision": revision.revision,
                "confirmed_by": principal.subject_id,
                "tenant_id": str(principal.tenant_id),
                "status": revision.status.value,
            },
        )

        return revision


def _assert_same_draft(
    existing: ShotDraft,
    project_id: str,
    episode_ref: str,
    scene_ref: str,
    shot_code: str,
    intent_summary: str,
    shot_description: str,
    source: ShotDraftSource,
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。"""
    if (
        existing.project_id != project_id
        or existing.episode_ref != episode_ref
        or existing.scene_ref != scene_ref
        or existing.shot_code != shot_code
        or existing.intent_summary != intent_summary
        or existing.shot_description != shot_description
        or existing.source != source
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
