"""ShotBreakdownDraft 应用服务（拆镜草案的诞生、确认、退回、废弃）。

本服务是镜头域唯一的事务链入口：
    草案 → 确认（认领 + 落定） → Shot + 首个 ShotRevision

确认的两跳为什么必须同事务（ADR-R40 §4）
--------------------------------------------------
PENDING_CONFIRM→IN_REVIEW 是「认领」，IN_REVIEW→CONFIRMED 是「落定」。
认领的作用是抢占：两个审核人同时确认时，第二个会在认领那一步撞乐观锁。
若两跳分成两个事务，第一个事务提交后进程崩溃，草案会永久停在 IN_REVIEW，
而没有任何人能再确认它——这比「两人同时确认」更糟。

Shot 与 ShotRevision 的原子创建（ADR-R18 §12）
--------------------------------------------------
首次确认产出一个 Shot + 首个 ShotRevision；两者必须同事务，否则会出现
「镜头已存在但没有任何修订」的悬空对象（Shot.shot_revision_id 无处可指）。

Scene 为什么必须预先存在（ADR-R19 §121）
--------------------------------------------------
本服务**不**在确认时隐式创建场次。草案里的 scene_id 必须指向一条已存在的
Scene（由 SCENE.CREATE 登记）；否则等于替所有者裁定「确认拆镜即建立正式场次」，
而 R19 明确拒绝了这个语义扩张。

唯一冲突必须被解释，不能冒成 500（ADR-R40 §6.4）
--------------------------------------------------
本服务是全项目唯一**批量**写入镜头编号的路径，而编号是用户可见的业务键——
冲突是常态，不是异常。端口层对唯一冲突只给一个异常（``DuplicateEntityError``），
但这里有两个成因、两种修复方向：草案内重号（改草案）、项目内已被占用（换编号）。
所以异常在服务层被翻译成两种带具体占用者信息的 409，而不是让它冒到 API 层变成
500——500 的含义是「服务端坏了」，而这件事的修法在调用方手里。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import utcnow
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import (
    ShotDraftStatus,
    ShotRevisionStatus,
    ShotStatus,
)
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.domain.state_machine import (
    SHOT_DRAFT_CONFIRM_ACTION,
    SHOT_DRAFT_DISCARD_ACTION,
    SHOT_DRAFT_RETURN_ACTION,
)
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    SceneRepository,
    ShotDraftRepository,
    ShotRepository,
    ShotRevisionRepository,
)
from services.project_planning.service.transition import advance_status

# 草案里每个镜头必须给出的字段（缺失即拒绝，不从别处猜默认值）。
# 这份清单就是 ShotRevision.required 去掉「由系统生成」的部分：id、revision_no、
# basis_script_revision_id、confirmed_by/at 由本服务填，其余必须由拆镜产出提供。
_SPEC_REQUIRED_TEXT = (
    "shot_code",
    "description",
    "narrative_intent",
    "location_ref",
    "constraint_pack_applicability",
    "creative_autonomy",
    "quality_profile",
    "change_origin",
)
_SPEC_REQUIRED_JSON = ("character_refs", "prop_refs", "risk", "intent_ref", "script_anchors")


class ShotDraftService:
    def __init__(
        self,
        repository: ShotDraftRepository,
        shot_repository: ShotRepository,
        revision_repository: ShotRevisionRepository,
        scene_repository: SceneRepository,
        project_repository: ProjectRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._shot_repository = shot_repository
        self._revision_repository = revision_repository
        self._scene_repository = scene_repository
        self._project_repository = project_repository
        self._outbox_writer = outbox_writer

    async def create_draft(
        self,
        *,
        project_id: str,
        basis_script_revision_id: str,
        plan_name: str,
        generation_method: str,
        scene_breakdowns: list[Any],
        breakdown_scope: dict[str, Any],
        created_by: str,
        tenant_id: UUID,
        episode_ref: str | None = None,
        supersedes_draft_ref: str | None = None,
        change_summary: str | None = None,
        ai_rationale: str | None = None,
        open_questions: list[Any] | None = None,
        potential_issues: list[Any] | None = None,
        entity_id: str | None = None,
    ) -> ShotBreakdownDraft:
        project_id = project_id.strip()
        basis_script_revision_id = basis_script_revision_id.strip()
        plan_name = plan_name.strip()
        generation_method = generation_method.strip()
        created_by = created_by.strip()
        for name, value in (
            ("project_id", project_id),
            ("basis_script_revision_id", basis_script_revision_id),
            ("plan_name", plan_name),
            ("generation_method", generation_method),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if not scene_breakdowns:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="scene_breakdowns 不能为空"
            )

        project = await self._project_repository.get_by_id(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}"
            )

        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_draft(existing, project_id, basis_script_revision_id, plan_name)
                return existing

        draft_id = entity_id or uuid.uuid4().hex
        draft = ShotBreakdownDraft(
            entity_id=draft_id,
            project_id=project_id,
            episode_ref=episode_ref,
            basis_script_revision_id=basis_script_revision_id,
            breakdown_scope=breakdown_scope,
            plan_name=plan_name,
            generation_method=generation_method,
            draft_version=1,
            # 首版以自身为链根；后续版本由 supersedes_draft_ref 串起。
            # 必须在构造时给定：root_draft_ref 是聚合根的非空不变量，
            # 先造一个空值再回填会让「对象存在过但不合法」成为可能。
            root_draft_ref=supersedes_draft_ref or draft_id,
            supersedes_draft_ref=supersedes_draft_ref,
            change_summary=change_summary,
            ai_rationale=ai_rationale,
            open_questions=list(open_questions or []),
            potential_issues=list(potential_issues or []),
            scene_breakdowns=list(scene_breakdowns),
            created_by=created_by,
            tenant_id=tenant_id,
        )

        try:
            await self._repository.add(draft)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="草案归属的项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            existing = (
                await self._repository.get_by_id(entity_id)
                if entity_id is not None
                else None
            )
            if existing is not None:
                _assert_same_draft(
                    existing, project_id, basis_script_revision_id, plan_name
                )
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="拆镜草案写入冲突"
            ) from exc

        self._outbox_writer.record(
            aggregate_type="ShotBreakdownDraft",
            aggregate_id=draft.entity_id,
            event_type="SHOT_DRAFT_CREATED",
            tenant_id=tenant_id,
            payload={
                "draft_id": draft.entity_id,
                "project_id": draft.project_id,
                "basis_script_revision_id": draft.basis_script_revision_id,
                "draft_version": draft.draft_version,
                "created_by": draft.created_by,
                "tenant_id": str(tenant_id),
                "status": draft.status.value,
            },
        )
        return draft

    async def get_draft(self, entity_id: str) -> ShotBreakdownDraft:
        draft = await self._repository.get_by_id(entity_id)
        if draft is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"拆镜草案不存在: {entity_id}"
            )
        return draft

    async def confirm_draft(
        self, *, entity_id: str, confirmed_by: str, tenant_id: UUID
    ) -> ShotBreakdownDraft:
        """人工确认：认领 → 落定 → 产出 Shot + ShotRevision（同一事务）。"""
        confirmed_by = confirmed_by.strip()
        if not confirmed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="confirmed_by 不能为空"
            )
        draft = await self._repository.get_by_id(entity_id)
        if draft is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"拆镜草案不存在: {entity_id}"
            )
        if draft.status is not ShotDraftStatus.PENDING_CONFIRM and (
            draft.status is not ShotDraftStatus.IN_REVIEW
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"草案处于终态 {draft.status.value}，不可原地复活；请基于新版本重新确认",
            )

        # 第一跳：认领。并发确认时第二个请求在这里撞乐观锁。
        if draft.status is ShotDraftStatus.PENDING_CONFIRM:
            await advance_status(
                draft,
                ShotDraftStatus.IN_REVIEW,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="ShotBreakdownDraft",
                event_type="SHOT_DRAFT_CLAIMED",
                tenant_id=tenant_id,
                payload={"draft_id": draft.entity_id, "project_id": draft.project_id},
                resource_type="ShotBreakdownDraft",
                owner_action=SHOT_DRAFT_CONFIRM_ACTION,
                conflict_detail="草案已被他人认领，请刷新后重试",
                actor_field="claimed_by",
                actor=confirmed_by,
            )

        confirmed_at = utcnow()
        revision_refs = await self._materialize_shots(
            draft, confirmed_by=confirmed_by, confirmed_at=confirmed_at, tenant_id=tenant_id
        )

        draft.confirmed_by = confirmed_by
        draft.confirmed_at = confirmed_at
        draft.confirmed_shot_revision_refs = revision_refs

        # 第二跳：落定。确认事件的 payload 必须带产出清单——「确认了什么」
        # 不是靠事后查 Shot 表反推出来的。
        await advance_status(
            draft,
            ShotDraftStatus.CONFIRMED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ShotBreakdownDraft",
            event_type="SHOT_DRAFT_CONFIRMED",
            tenant_id=tenant_id,
            payload={
                "draft_id": draft.entity_id,
                "project_id": draft.project_id,
                "confirmed_by": confirmed_by,
                "confirmed_shot_revision_refs": revision_refs,
            },
            resource_type="ShotBreakdownDraft",
            owner_action=SHOT_DRAFT_CONFIRM_ACTION,
            conflict_detail="草案确认并发冲突，请刷新后重试",
            actor_field="confirmed_by",
            actor=confirmed_by,
        )
        return draft

    async def return_draft(
        self,
        *,
        entity_id: str,
        returned_by: str,
        return_reason: str,
        tenant_id: UUID,
    ) -> ShotBreakdownDraft:
        draft = await self._require_draft(entity_id)
        draft.returned_by = returned_by.strip()
        draft.returned_at = utcnow()
        draft.return_reason = return_reason.strip()
        await advance_status(
            draft,
            ShotDraftStatus.RETURNED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ShotBreakdownDraft",
            event_type="SHOT_DRAFT_RETURNED",
            tenant_id=tenant_id,
            payload={
                "draft_id": draft.entity_id,
                "project_id": draft.project_id,
                "returned_by": draft.returned_by,
                "return_reason": draft.return_reason,
            },
            resource_type="ShotBreakdownDraft",
            owner_action=SHOT_DRAFT_RETURN_ACTION,
            conflict_detail="草案退回并发冲突，请刷新后重试",
            actor_field="returned_by",
            actor=draft.returned_by,
        )
        return draft

    async def discard_draft(
        self,
        *,
        entity_id: str,
        discarded_reason: str,
        changed_by: str,
        tenant_id: UUID,
    ) -> ShotBreakdownDraft:
        draft = await self._require_draft(entity_id)
        draft.discarded_reason = discarded_reason.strip()
        await advance_status(
            draft,
            ShotDraftStatus.DISCARDED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ShotBreakdownDraft",
            event_type="SHOT_DRAFT_DISCARDED",
            tenant_id=tenant_id,
            payload={
                "draft_id": draft.entity_id,
                "project_id": draft.project_id,
                "discarded_reason": draft.discarded_reason,
            },
            resource_type="ShotBreakdownDraft",
            owner_action=SHOT_DRAFT_DISCARD_ACTION,
            conflict_detail="草案废弃并发冲突，请刷新后重试",
            actor_field="discarded_by",
            actor=changed_by,
        )
        return draft

    async def _require_draft(self, entity_id: str) -> ShotBreakdownDraft:
        draft = await self._repository.get_by_id(entity_id)
        if draft is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"拆镜草案不存在: {entity_id}"
            )
        return draft

    async def _materialize_shots(
        self,
        draft: ShotBreakdownDraft,
        *,
        confirmed_by: str,
        confirmed_at: datetime,
        tenant_id: UUID,
    ) -> list[str]:
        """按草案产出 Shot + 首个 ShotRevision，返回修订 id 清单。

        全部写操作都在当前事务内；任何一步失败都会让整个确认回滚，
        不会留下「部分镜头已建、草案仍待确认」的中间状态。
        """
        revision_refs: list[str] = []
        for index, block in enumerate(draft.scene_breakdowns):
            if not isinstance(block, dict):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"scene_breakdowns[{index}] 必须是对象",
                )
            scene_id = _required_text(block, "scene_id", f"scene_breakdowns[{index}]")
            scene = await self._scene_repository.get_by_id(scene_id)
            if scene is None or scene.project_id != draft.project_id:
                raise DomainError(
                    ErrorRegistry.NOT_FOUND,
                    detail=(
                        f"草案引用的场次不存在或不属于本项目: {scene_id}。"
                        "场次必须先由 SCENE.CREATE 登记（ADR-R19 §121）"
                    ),
                )

            shots = block.get("shots")
            if not isinstance(shots, list) or not shots:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"scene_breakdowns[{index}].shots 不能为空",
                )
            for shot_index, spec in enumerate(shots):
                where = f"scene_breakdowns[{index}].shots[{shot_index}]"
                if not isinstance(spec, dict):
                    raise DomainError(
                        ErrorRegistry.VALIDATION_ERROR, detail=f"{where} 必须是对象"
                    )
                revision_refs.append(
                    await self._create_shot_with_revision(
                        draft,
                        scene_id=scene_id,
                        episode_id=scene.episode_id,
                        spec=spec,
                        where=where,
                        confirmed_by=confirmed_by,
                        confirmed_at=confirmed_at,
                        tenant_id=tenant_id,
                    )
                )
        return revision_refs

    async def _create_shot_with_revision(
        self,
        draft: ShotBreakdownDraft,
        *,
        scene_id: str,
        episode_id: str,
        spec: dict[str, Any],
        where: str,
        confirmed_by: str,
        confirmed_at: datetime,
        tenant_id: UUID,
    ) -> str:
        text = {key: _required_text(spec, key, where) for key in _SPEC_REQUIRED_TEXT}
        for key in _SPEC_REQUIRED_JSON:
            if key not in spec:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{where}.{key} 缺失"
                )

        shot_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex

        # 先建 Shot 再建 Revision：Revision 的复合外键指向 shots，
        # 反过来不成立（Shot 只持有 revision 的 id 值，不是外键）。
        shot = Shot(
            entity_id=shot_id,
            scene_id=scene_id,
            project_id=draft.project_id,
            shot_code=text["shot_code"],
            shot_revision_id=revision_id,
            status=ShotStatus.NOT_STARTED,
            source_draft_ref={
                "draft_id": draft.entity_id,
                "draft_version": draft.draft_version,
            },
            created_by=confirmed_by,
            tenant_id=tenant_id,
        )
        try:
            await self._shot_repository.add(shot)
        except DuplicateEntityError as exc:
            # 唯一冲突的成因必须回读后解释：草案内重号（改草案）与项目内已被
            # 占用（换编号）都是 409，但修复方向相反（ADR-R40 §6.4）。
            # 未捕获时这一支会冒成 500——拆镜确认是本轮唯一会批量写入镜头
            # 编号的路径，而编号是用户可见的业务键，冲突是常态而非异常。
            conflict = await self._shot_code_conflict(draft, shot.shot_code)
            raise conflict from exc
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="镜头归属的场次已变化，请基于最新数据重试",
            ) from exc

        revision = ShotRevision(
            entity_id=revision_id,
            shot_id=shot_id,
            project_id=draft.project_id,
            episode_id=episode_id,
            scene_id=scene_id,
            revision_no=1,
            description=text["description"],
            narrative_intent=text["narrative_intent"],
            character_refs=list(spec["character_refs"]),
            location_ref=text["location_ref"],
            prop_refs=list(spec["prop_refs"]),
            constraint_pack_applicability=text["constraint_pack_applicability"],
            constraint_pack_id=spec.get("constraint_pack_id"),
            creative_autonomy=text["creative_autonomy"],
            risk=dict(spec["risk"]),
            quality_profile=text["quality_profile"],
            intent_ref=dict(spec["intent_ref"]),
            basis_script_revision_id=draft.basis_script_revision_id,
            script_anchors=list(spec["script_anchors"]),
            source_draft_ref={"draft_id": draft.entity_id},
            source_candidate_refs=list(spec.get("source_candidate_refs") or []),
            change_origin=text["change_origin"],
            change_summary=spec.get("change_summary"),
            changed_fields=list(spec.get("changed_fields") or []),
            provenance=dict(spec.get("provenance") or {}),
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            status=ShotRevisionStatus.ACTIVE,
            created_by=confirmed_by,
            tenant_id=tenant_id,
        )
        try:
            await self._revision_repository.add(revision)
        except DuplicateEntityError as exc:
            # 每个 Shot 恰一个 ACTIVE 修订（ux_shot_active_revision）。库里出现
            # 第二个，说明另一个事务刚刚为同一镜头确认了修订——这是并发，不是
            # 数据错误，重试语义正确。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=(
                    f"镜头 {shot_id} 的修订已被并发确认"
                    "（同一镜头只允许一个 ACTIVE 修订），请刷新后重试"
                ),
            ) from exc
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="镜头修订归属的镜头已变化，请基于最新数据重试",
            ) from exc

        self._outbox_writer.record(
            aggregate_type="Shot",
            aggregate_id=shot.entity_id,
            event_type="SHOT_CREATED",
            tenant_id=tenant_id,
            payload={
                "shot_id": shot.entity_id,
                "project_id": shot.project_id,
                "scene_id": shot.scene_id,
                "shot_code": shot.shot_code,
                "shot_revision_id": shot.shot_revision_id,
                "created_by": confirmed_by,
                "tenant_id": str(tenant_id),
                "status": shot.status.value,
            },
        )
        self._outbox_writer.record(
            aggregate_type="ShotRevision",
            aggregate_id=revision.entity_id,
            event_type="SHOT_REVISION_CONFIRMED",
            tenant_id=tenant_id,
            payload={
                "shot_revision_id": revision.entity_id,
                "shot_id": revision.shot_id,
                "project_id": revision.project_id,
                "revision_no": revision.revision_no,
                "confirmed_by": confirmed_by,
                "tenant_id": str(tenant_id),
                "status": revision.status.value,
            },
        )
        return revision.entity_id

    async def _shot_code_conflict(
        self, draft: ShotBreakdownDraft, shot_code: str
    ) -> DomainError:
        """把镜头编号的唯一冲突解释成调用方能执行的修复动作（ADR-R40 §6.4）。

        为什么必须回读一次：端口层对**所有**唯一冲突都给同一个异常，而这条路径
        上有两个成因——草案内编号重复（改草案）与项目内编号已被别的镜头占用
        （换编号）。两者都是 409，但修复方向相反；不回读就只能给「重复提交」
        这种听上去像重试就好的说法，而重试必然继续失败。

        占用者若来自本草案自己（同一事务内先建的镜头），说明是草案内部重号；
        否则是本项目已存在的镜头。区分这两个来源不需要额外查询：占用行自带
        ``source_draft_ref``。
        """
        taken = await self._shot_repository.get_by_project_code(
            draft.project_id, shot_code
        )
        if taken is None:
            return DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"镜头编号 {shot_code} 写入冲突，且回读不到占用者",
            )
        source = taken.source_draft_ref if isinstance(taken.source_draft_ref, dict) else {}
        if source.get("draft_id") == draft.entity_id:
            origin = "本草案内编号重复"
        else:
            origin = f"占用者 scene_id={taken.scene_id}"
        return DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=(
                f"镜头编号 {shot_code} 在项目内已被占用"
                f"（SH-3：项目内唯一，{origin}）。请修订草案后重新提交确认"
            ),
        )


def _required_text(container: dict[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR, detail=f"{where}.{key} 缺失或为空"
        )
    return value.strip()


def _assert_same_draft(
    existing: ShotBreakdownDraft,
    project_id: str,
    basis_script_revision_id: str,
    plan_name: str,
) -> None:
    if (
        existing.project_id != project_id
        or existing.basis_script_revision_id != basis_script_revision_id
        or existing.plan_name != plan_name
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
