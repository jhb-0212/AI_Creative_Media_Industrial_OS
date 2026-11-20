"""Scene 应用服务（场次登记与状态推进）。

与 EpisodeService 同型，刻意不另造机制：
- 幂等：显式 entity_id 重复登记回读；内容不一致 409。
- 归属验证：场次必须挂到真实存在的集数，且集数所属项目必须等于 project_id。
  这条校验在服务层做一次，库层的复合外键再锁一次——服务层给可读错误，
  库层防绕过。
- 编号唯一：SC-3 要求 scene_code 在**项目内**唯一，因此幂等回读键是
  (project_id, scene_code)，与唯一约束同口径；跨集撞号按「编号被占用」报错，
  不报成幂等冲突。
- 归档前置条件：SC-5 要求归档前确认其下不存在 IN_PRODUCTION 的镜头，因此在
  状态推进前查一次；这是本服务唯一跨聚合读，代价是必要的（见 change_status）。
- 登记与 SCENE_CREATED 同事务写入；状态推进复用 advance_status 唯一路径。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.state_machine import SCENE_STATUS_CHANGE_ACTION
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.shot_planning_repositories import (
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.transition import advance_status


class SceneService:
    def __init__(
        self,
        repository: SceneRepository,
        episode_repository: EpisodeRepository,
        outbox_writer: OutboxWriter,
        shot_repository: ShotRepository,
    ) -> None:
        self._repository = repository
        self._episode_repository = episode_repository
        self._outbox_writer = outbox_writer
        self._shot_repository = shot_repository

    async def create_scene(
        self,
        *,
        project_id: str,
        episode_id: str,
        scene_code: str,
        created_by: str,
        tenant_id: UUID,
        entity_id: str | None = None,
    ) -> Scene:
        project_id = project_id.strip()
        episode_id = episode_id.strip()
        scene_code = scene_code.strip()
        created_by = created_by.strip()
        for name, value in (
            ("project_id", project_id),
            ("episode_id", episode_id),
            ("scene_code", scene_code),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        # 归属验证：集数必须存在，且属于声明的项目（防止跨项目挂载）。
        episode = await self._episode_repository.get_by_id(episode_id)
        if episode is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"集数不存在: {episode_id}"
            )
        if episode.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="集数不属于声明的项目（跨项目挂载被拒绝）",
            )

        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, project_id, episode_id, scene_code)
                return existing

        scene = Scene(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            episode_id=episode_id,
            scene_code=scene_code,
            created_by=created_by,
            tenant_id=tenant_id,
        )

        try:
            await self._repository.add(scene)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="场次归属的集数已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            if entity_id is not None:
                existing = await self._repository.get_by_id(entity_id)
                if existing is not None:
                    _assert_same_request(existing, project_id, episode_id, scene_code)
                    return existing
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR, detail="场次写入冲突"
                ) from exc
            # 未带幂等键却撞唯一约束：唯一可能来源是编号已被占用（SC-3 项目内唯一）。
            # 这不是「同一个幂等键复用」，报成幂等冲突会把调用方引向错误的修复
            # 方向（重试不会成功，必须换编号）。
            taken = await self._repository.get_by_project_code(project_id, scene_code)
            if taken is None:
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR, detail="场次写入冲突"
                ) from exc
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"场次编号 {scene_code} 在项目内已被占用"
                    f"（SC-3：项目内唯一，占用者 episode_id={taken.episode_id}）"
                ),
            ) from exc

        self._outbox_writer.record(
            aggregate_type="Scene",
            aggregate_id=scene.entity_id,
            event_type="SCENE_CREATED",
            tenant_id=tenant_id,
            payload={
                "scene_id": scene.entity_id,
                "project_id": project_id,
                "episode_id": episode_id,
                "scene_code": scene_code,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
                "status": scene.status.value,
            },
        )
        return scene

    async def get_scene(self, entity_id: str) -> Scene:
        scene = await self._repository.get_by_id(entity_id)
        if scene is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {entity_id}")
        return scene

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: SceneStatus,
        changed_by: str,
        tenant_id: UUID,
    ) -> Scene:
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )
        scene = await self._repository.get_by_id(entity_id)
        if scene is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {entity_id}")

        # SC-5 归档前置条件：其下不得存在 IN_PRODUCTION 的镜头。
        # 为什么「终态无出边」不够：边表只保证归档之后不被复活，不保证归档本身
        # 合法。若带着在产镜头归档，那些镜头会永久失去可推进的父级——这不是
        # 「以后修一下」，是状态库里出现无法收敛的对象。
        if (
            target_status is SceneStatus.ARCHIVED
            and scene.status is not SceneStatus.ARCHIVED
            and await self._shot_repository.has_with_status(
                scene.scene_id, ShotStatus.IN_PRODUCTION
            )
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="场次下仍有 IN_PRODUCTION 的镜头，拒绝归档（SC-5）",
            )

        await advance_status(
            scene,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="Scene",
            event_type="SCENE_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "scene_id": scene.entity_id,
                "project_id": scene.project_id,
            },
            resource_type="Scene",
            owner_action=SCENE_STATUS_CHANGE_ACTION,
            conflict_detail="场次状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
        )
        return scene


def _assert_same_request(
    existing: Scene, project_id: str, episode_id: str, scene_code: str
) -> None:
    if (
        existing.project_id != project_id
        or existing.episode_id != episode_id
        or existing.scene_code != scene_code
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
