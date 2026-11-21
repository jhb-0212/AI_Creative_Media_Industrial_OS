"""Shot 应用服务（CONFIRM + STATUS.CHANGE 成对落地）。

本服务刻意**没有** ``create_shot``：契约 SHOT.REVISION.CONFIRM 是正式镜头唯一
入口，首次确认在同一事务里原子创建 Shot 与首个 ShotRevision；后续确认只新增
修订、把旧版推 SUPERSEDED 并前移 Shot 指针。开一个 SHOT.CREATE 后门会让
「先建空镜头、后补内容」成为合法路径，而空镜头没有可引用的正式版本。

版本冲突按 SR-3 fail-closed：
- 首次确认要求 shot_id 不存在；
- 后续确认要求 ``expected_shot_revision_id`` 精确等于 Shot 当前指针；
- revision_no 由服务端从 ACTIVE head 派生，拒绝调用方自带版本号；
- 乐观锁再兜一次并发窗口。

取消（CANCELLED）的三条边在领域与数据库都有守卫字段，但对应的「授权记录」
对象尚无 schema/仓储；这里显式拒绝，不用一个不可解析的字符串把无据取消写成
既成事实。契约状态机仍登记这三条边，运行时行为是 fail-closed，不是静默缺失。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import utcnow
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ShotRevisionStatus,
    ShotStatus,
)
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    PROJECT_LIFECYCLE,
    SCENE_LIFECYCLE,
    SHOT_REVISION_CONFIRM_ACTION,
    SHOT_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.scene_repo import SceneRepository
from services.project_planning.repository.shot_repo import (
    ReworkTaskVerifier,
    ShotRepository,
    ShotRevisionRepository,
)
from services.project_planning.service.transition import (
    advance_status,
    save_versioned_entity,
)


@dataclass(frozen=True)
class ConfirmRevisionCommand:
    """SHOT.REVISION.CONFIRM 的用例输入（身份由 Principal 提供，不在载荷里）。"""

    project_id: str
    scene_id: str
    shot_code: str
    description: str
    narrative_intent: str
    location_ref: str
    basis_script_revision_id: str
    script_anchors: tuple[str, ...]
    character_refs: tuple[str, ...] = ()
    prop_refs: tuple[str, ...] = ()
    constraint_pack_applicability: str = "NOT_APPLICABLE"
    constraint_pack_id: str | None = None
    creative_autonomy: str = "L1"
    risk: dict[str, Any] | None = None
    quality_profile: str = "UNIFIED_HIGH_QUALITY"
    intent_ref: dict[str, Any] | None = None
    source_draft_ref: dict[str, Any] | None = None
    source_candidate_refs: tuple[str, ...] = ()
    change_origin: str = "BREAKDOWN_CONFIRM"
    change_summary: str | None = None
    changed_fields: tuple[str, ...] = ()
    provenance: dict[str, Any] | None = None
    shot_id: str | None = None
    expected_shot_revision_id: str | None = None


class ShotService:
    """镜头确认与状态推进用例。"""

    def __init__(
        self,
        shots: ShotRepository,
        revisions: ShotRevisionRepository,
        scenes: SceneRepository,
        episodes: EpisodeRepository,
        projects: ProjectRepository,
        rework_tasks: ReworkTaskVerifier,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._shots = shots
        self._revisions = revisions
        self._scenes = scenes
        self._episodes = episodes
        self._projects = projects
        self._rework_tasks = rework_tasks
        self._outbox = outbox_writer

    async def confirm_revision(
        self,
        command: ConfirmRevisionCommand,
        *,
        confirmed_by: str,
        tenant_id: UUID,
    ) -> tuple[Shot, ShotRevision]:
        """人类确认镜头内容（唯一正式写入口）。"""
        confirmed_by = confirmed_by.strip()
        if not confirmed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="confirmed_by 不能为空"
            )

        project, episode, scene = await self._lock_ancestors(
            command.project_id, command.scene_id
        )
        if project.organization_id is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="项目缺少组织归属，无法确认正式镜头",
            )
        self._assert_ancestors_open(project, episode.status, scene)

        existing = (
            await self._shots.get_by_id(command.shot_id)
            if command.shot_id is not None
            else None
        )
        head = (
            await self._revisions.active_head(existing.shot_id)
            if existing is not None
            else None
        )

        # 首版重放：同 shot_id + 当前版本 + 同一份内容 ⇒ 返回原结果，不新增写。
        # 内容不同则按「新修订」继续（下面会用 revision_no+1 与乐观锁保证不覆盖）。
        if existing is not None:
            self._assert_same_shot(existing, command, episode.entity_id, scene.entity_id)
            if head is None:
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR,
                    detail="镜头缺少 ACTIVE 修订指针，拒绝继续写入",
                )
            if command.expected_shot_revision_id != head.entity_id:
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail="版本冲突：expected_shot_revision_id 不是当前 ACTIVE 版本",
                )
            if self._same_revision_intent(head, command):
                return existing, head
        elif command.shot_id is not None or command.expected_shot_revision_id is not None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail="后续修订必须指向已确认镜头及其 ACTIVE 版本",
            )

        detected = await self._shots.get_by_project_code(
            project.entity_id, command.shot_code
        )
        if detected is not None and (existing is None or detected.entity_id != existing.entity_id):
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail="同一项目内 shot_code 已存在，须用 shot_id + expected 版本推进修订",
            )

        confirmed_at = utcnow()
        shot_id = command.shot_id or uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        revision_no = 1 if head is None else head.revision_no + 1
        if await self._revisions.get_by_revision_no(shot_id, revision_no):
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"revision_no={revision_no} 已存在，拒绝并发创建同版修订",
            )

        revision = ShotRevision(
            entity_id=revision_id,
            created_by=confirmed_by,
            tenant_id=tenant_id,
            shot_id=shot_id,
            project_id=project.entity_id,
            episode_id=episode.entity_id,
            scene_id=scene.entity_id,
            episode_ref=episode.episode_ref,
            scene_ref=scene.scene_code,
            revision_no=revision_no,
            description=command.description,
            narrative_intent=command.narrative_intent,
            character_refs=list(command.character_refs),
            location_ref=command.location_ref,
            prop_refs=list(command.prop_refs),
            constraint_pack_applicability=command.constraint_pack_applicability,
            constraint_pack_id=command.constraint_pack_id,
            creative_autonomy=command.creative_autonomy,
            risk=dict(command.risk or {"level": "LOW"}),
            quality_profile=command.quality_profile,
            intent_ref=dict(command.intent_ref or {}),
            basis_script_revision_id=command.basis_script_revision_id,
            script_anchors=list(command.script_anchors),
            source_draft_ref=command.source_draft_ref,
            source_candidate_refs=list(command.source_candidate_refs),
            change_origin=command.change_origin,
            supersedes_shot_revision_id=head.entity_id if head is not None else None,
            change_summary=command.change_summary,
            changed_fields=list(command.changed_fields),
            provenance=dict(command.provenance or {}),
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            organization_id=project.organization_id,
        )

        try:
            await self._revisions.add(revision)
        except (DuplicateEntityError, ReferentialIntegrityError) as exc:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail="修订身份或引用版本冲突，请基于最新 ACTIVE 版本重试",
            ) from exc

        confirm_payload = {
            "shot_id": shot_id,
            "shot_revision_id": revision.entity_id,
            "project_id": project.entity_id,
            "scene_id": scene.entity_id,
            "revision_no": revision_no,
            "confirmed_by": confirmed_by,
            "tenant_id": str(tenant_id),
        }

        if head is not None and existing is not None:
            await advance_status(
                head,
                ShotRevisionStatus.SUPERSEDED,
                repository=self._revisions,
                outbox_writer=self._outbox,
                aggregate_type="ShotRevision",
                event_type="SHOT_REVISION_SUPERSEDED",
                tenant_id=tenant_id,
                payload={
                    "shot_id": shot_id,
                    "shot_revision_id": head.entity_id,
                    "superseded_by": revision.entity_id,
                    "project_id": project.entity_id,
                    "confirmed_by": confirmed_by,
                },
                resource_type="ShotRevision",
                owner_action=SHOT_REVISION_CONFIRM_ACTION,
                conflict_detail="旧版修订已被并发取代，请基于最新版本重试",
                actor_field="confirmed_by",
                actor=confirmed_by,
                organization_id=project.organization_id,
            )
            def point_to_new_revision(audit_ref: str, action: str) -> None:
                existing.point_to_revision(revision.entity_id, audit_ref, action)

            # 版本指针前移不是状态迁移（事实表禁止 from_status == to_status），
            # 但必须经同一个审计落库入口，否则会留下「指针变了、事件没写」的缺口。
            await save_versioned_entity(
                existing,
                mutation=point_to_new_revision,
                audit_ref=f"revision:{revision_id}",
                repository=self._shots,
                outbox_writer=self._outbox,
                aggregate_type="ShotRevision",
                aggregate_id=revision.entity_id,
                event_type="SHOT_REVISION_CONFIRMED",
                tenant_id=tenant_id,
                payload=confirm_payload,
                owner_action=SHOT_REVISION_CONFIRM_ACTION,
                conflict_detail="镜头版本指针已被并发前移，请重试",
            )
            shot = existing
        else:
            shot = Shot(
                entity_id=shot_id,
                created_by=confirmed_by,
                tenant_id=tenant_id,
                shot_code=command.shot_code,
                scene_id=scene.entity_id,
                project_id=project.entity_id,
                shot_revision_id=revision.entity_id,
                source_draft_ref=command.source_draft_ref,
                organization_id=project.organization_id,
            )
            try:
                await self._shots.add(shot)
            except (DuplicateEntityError, ReferentialIntegrityError) as exc:
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail="镜头身份或归属冲突，请基于最新状态重试",
                ) from exc

            self._outbox.record(
                aggregate_type="ShotRevision",
                aggregate_id=revision.entity_id,
                tenant_id=tenant_id,
                event_type="SHOT_REVISION_CONFIRMED",
                payload=confirm_payload,
            )
        return shot, revision

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
        """按 SHOT.STATUS.CHANGE 推进镜头状态（唯一状态写入路径）。"""
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )
        if target_status is ShotStatus.CANCELLED:
            # 授权记录对象没有 schema/仓储；写一个无法解析的 ref 等于无据取消。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="取消授权记录对象未建模，CANCELLED 路径 fail-closed 拒绝",
            )

        shot = await self._shots.get_by_id(entity_id)
        if shot is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头不存在: {entity_id}")

        is_rework = (
            shot.status is ShotStatus.PRODUCTION_COMPLETE
            and target_status is ShotStatus.IN_PRODUCTION
        )
        if is_rework:
            if not (rework_basis_ref and rework_basis_ref.strip()):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="PRODUCTION_COMPLETE 回退必须携带正式返工依据",
                )
            exists = await self._rework_tasks.exists_for_shot(
                rework_basis_ref,
                project_id=shot.project_id,
                tenant_id=tenant_id,
                organization_id=shot.organization_id,
            )
            if not exists:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="返工依据不存在或不属于同一项目/租户/组织",
                )

        def domain_operation(audit_ref: str, action: str) -> None:
            shot.apply_status_change(
                target_status,
                audit_ref,
                action,
                blocked_reason=blocked_reason,
                rework_basis_ref=rework_basis_ref,
                cancellation_ref=cancellation_ref,
                cancelled_at=cancelled_at,
            )

        await advance_status(
            shot,
            target_status,
            repository=self._shots,
            outbox_writer=self._outbox,
            aggregate_type="Shot",
            event_type="SHOT_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "shot_id": shot.shot_id,
                "project_id": shot.project_id,
                "scene_id": shot.scene_id,
            },
            resource_type="Shot",
            owner_action=SHOT_STATUS_CHANGE_ACTION,
            conflict_detail="镜头状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            organization_id=shot.organization_id,
            domain_operation=domain_operation,
        )
        return shot

    async def _lock_ancestors(
        self, project_id: str, scene_id: str
    ) -> tuple[Project, Episode, Scene]:
        """自顶向下锁定 Project → Episode → Scene 并验证真实归属。

        父行查不到必须拒绝：RLS 错租户时 FOR SHARE 静默返回 0 行，若写成
        ``if parent`` 就会把越权写入伪装成「父不存在」。
        """
        # 先做一次无锁读只为解析 episode_ref（场景只存 episode_id）；真正的
        # 判定仍用下面三把 FOR SHARE。无锁读不产生任何写入决策。
        scene_seed = await self._scenes.get_by_id(scene_id)
        if scene_seed is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {scene_id}"
            )

        project = await self._projects.get_by_id_for_child_write(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}"
            )

        episode_seed = await self._episodes.get_by_id(scene_seed.episode_id)
        if episode_seed is None or episode_seed.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"场次所属集不属于项目 {project_id}",
            )
        episode = await self._episodes.get_by_project_ref_for_child_write(
            project_id, episode_seed.episode_ref
        )
        if episode is None or episode.entity_id != scene_seed.episode_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"场次 {scene_id} 的集归属与项目不一致",
            )

        scene = await self._scenes.get_by_id_for_child_write(scene_id)
        if scene is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {scene_id}"
            )
        if scene.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"场次 {scene_id} 不属于项目 {project_id}",
            )
        return project, episode, scene

    @staticmethod
    def _assert_ancestors_open(
        project: Project, episode_status: EpisodeStatus, scene: Scene
    ) -> None:
        PROJECT_LIFECYCLE.assert_child_creation_allowed(
            project.status, child_object="Shot"
        )
        EPISODE_LIFECYCLE.assert_child_creation_allowed(
            episode_status, child_object="Shot"
        )
        SCENE_LIFECYCLE.assert_child_creation_allowed(scene.status, child_object="Shot")

    @staticmethod
    def _same_revision_intent(
        revision: ShotRevision, command: ConfirmRevisionCommand
    ) -> bool:
        """判断一次 CONFIRM 是否是刚成功的同版重放。

        只比「决定内容身份」的字段，不比时间戳等写入结果：重放请求拿不到
        服务端生成的 confirmed_at/revision_id，要求逐字段相等会永远判成新修订。
        """
        return (
            revision.description == command.description
            and revision.narrative_intent == command.narrative_intent
            and revision.location_ref == command.location_ref
            and revision.basis_script_revision_id == command.basis_script_revision_id
            and tuple(revision.script_anchors) == tuple(command.script_anchors)
            and tuple(revision.character_refs) == tuple(command.character_refs)
            and tuple(revision.prop_refs) == tuple(command.prop_refs)
            and revision.source_draft_ref == command.source_draft_ref
            and revision.change_origin == command.change_origin
        )

    @staticmethod
    def _assert_same_shot(
        existing: Shot, command: ConfirmRevisionCommand, episode_id: str, scene_id: str
    ) -> None:
        if (
            existing.project_id != command.project_id
            or existing.scene_id != scene_id
            or existing.shot_code != command.shot_code
        ):
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail="同一 shot_id 已存在且归属或编号不同",
            )


__all__ = ["ConfirmRevisionCommand", "ShotService"]
