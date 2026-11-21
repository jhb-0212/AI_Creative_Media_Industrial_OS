"""Scene 应用服务（场次登记与状态推进）。

两条历史实现线的**单一收敛点**（A13 运行时 + 归属治理线）：
- 场景登记（``SCENE.CREATE``）同时承载两类不变量，缺一不可：
  1. **归属治理线**：父链逐级冻结判定（Project → Episode）、组织归属键从父
     项目派生、幂等先于冻结、自顶向下固定锁序。这防止「上层已封存、下层还在
     长」与「归档后重试变 409」——两者在正常路径看不出来，只在特定时序暴露。
  2. **A13 运行时线**：SC-3 编号在项目内唯一（幂等回读键与库约束同口径）、
     结构化错误映射。
- 状态推进（``SCENE.STATUS.CHANGE``）：SC-5 归档前置条件——持排他行锁重判
  「其下无在产镜头」。这条前置条件与子对象写入共享数据却不共享约束，「先查
  后写」在并发下必被穿透，故锁定为「锁 → 重判 → 写」同一事务序列。

两个失败形态都不能只靠库层：父链冻结的可读 4xx、SC-5 的可读 4xx 需要服务层
给出，库层守卫负责拦绕过服务层的写入——纵深防御，不是二选一。

寻址口径（两条线的兼容面）
--------------------------
``create_scene`` 同时接受 ``episode_id``（身份键，A13/正式迁移链口径）与
``episode_ref``（业务键，归属治理链口径），二选一且互斥：
- 传 ``episode_ref``：走 ``get_by_project_ref_for_child_write`` 带锁读父链；
- 传 ``episode_id``：走 ``get_by_id`` 读集数。
两者最终都解析出真实的 ``episode`` 与 ``project``，冻结判定、组织键派生、
跨项目校验完全一致——寻址方式不同，治理语义同源。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.lifecycle import ChildPrecondition
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    PROJECT_LIFECYCLE,
    SCENE_LIFECYCLE,
    SCENE_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.transition import advance_status


class SceneService:
    """场次登记、状态推进与查询用例（两条实现线的单一权威）。"""

    def __init__(
        self,
        repository: SceneRepository,
        episode_repository: EpisodeRepository,
        outbox_writer: OutboxWriter,
        shot_repository: ShotRepository | None = None,
        project_repository: ProjectRepository | None = None,
    ) -> None:
        # 位置参数顺序保持 A13 运行时原样（repository, episode_repository,
        # outbox_writer, shot_repository），归属治理线所需的 project_repository
        # 作为第五个可选依赖追加——这样两条线的装配代码与测试替身都无需重排。
        self._repository = repository
        self._episode_repository = episode_repository
        self._outbox_writer = outbox_writer
        self._shot_repository = shot_repository
        self._project_repository = project_repository

    async def create_scene(
        self,
        *,
        project_id: str,
        scene_code: str,
        created_by: str,
        tenant_id: UUID,
        episode_id: str | None = None,
        episode_ref: str | None = None,
        entity_id: str | None = None,
    ) -> Scene:
        """登记场次：兼容双寻址，父链冻结 + 组织键 + 幂等先于冻结 + 编号唯一。

        两种寻址必须二选一且仅选其一——同时给出或都不给都是调用方缺陷，直接
        拒绝比「悄悄选一个」更安全（后者会让跨项目错配藏进被忽略的那个参数）。
        """
        project_id = project_id.strip()
        scene_code = scene_code.strip()
        created_by = created_by.strip()
        episode_id = episode_id.strip() if episode_id is not None else None
        episode_ref = episode_ref.strip() if episode_ref is not None else None

        for name, value in (
            ("project_id", project_id),
            ("scene_code", scene_code),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if (episode_id is None) == (episode_ref is None):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="episode_id 与 episode_ref 必须二选一",
            )

        project, episode, organization_id = await self._resolve_parents(
            project_id, episode_id, episode_ref
        )

        # 幂等：已存在同 id 直接返回，**刻意排在组织键与冻结判定之前**。
        # 重放一个当时成功过的请求是重试而非新建；若先判冻结，归档后的重试
        # 会变成 4xx，调用方将永远无法确认自己那次创建到底有没有生效。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, project_id, episode.entity_id, scene_code)
                return existing

        # 治理线（episode_ref 寻址）才在服务层做组织键与父链冻结检查。
        # A13 正式链（episode_id 寻址）把父链冻结放在数据库守卫，project 为
        # None——那条线上的组织归属由 RLS 承担，不在服务层复判（见 _resolve_parents）。
        if project is not None:
            if organization_id is None:
                # 场次的组织归属键在归属链表上是 NOT NULL，而个人租户项目没有组织键。
                # 给出可诊断的 4xx，不让它落到 23502 被翻译成「归属已变化」。
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        "项目缺少组织归属，无法登记场次："
                        "场次表要求 (project_id, tenant_id, organization_id) 三列成立"
                    ),
                )
            self._assert_ancestors_open_for_child_write(project, episode)

        scene = Scene(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            episode_id=episode.entity_id,
            scene_code=scene_code,
            created_by=created_by,
            tenant_id=tenant_id,
            organization_id=organization_id,
        )

        try:
            await self._repository.add(scene)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="场次归属的集或项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            # 三种冲突必须分开报——都是 409 但修复方向相反：
            # (a) 提供 entity_id 且已存在 → 幂等：同内容返回、异内容 409；
            # (b) 提供新的 entity_id 却撞编号 → 编号被占用（必须换编号）；
            # (c) 未提供 entity_id，按 (project, code) 回读占用者：同集同码是
            #     幂等重放，跨集同码是编号被占用（SC-3 项目内唯一）。
            if entity_id is not None:
                existing = await self._repository.get_by_id(entity_id)
                if existing is not None:
                    _assert_same_request(
                        existing, project_id, episode.entity_id, scene_code
                    )
                    return existing
                taken = await self._repository.get_by_project_code(
                    project_id, scene_code
                )
                if taken is not None:
                    raise DomainError(
                        ErrorRegistry.IDEMPOTENCY_CONFLICT,
                        detail=(
                            f"场次编号 {scene_code} 在项目内已被占用"
                            f"（SC-3：项目内唯一，占用者 episode_id={taken.episode_id}）"
                        ),
                    ) from exc
            else:
                taken = await self._repository.get_by_project_code(
                    project_id, scene_code
                )
                if taken is not None:
                    if taken.episode_id == episode.entity_id:
                        return taken
                    raise DomainError(
                        ErrorRegistry.IDEMPOTENCY_CONFLICT,
                        detail=(
                            f"场次编号 {scene_code} 在项目内已被占用"
                            f"（SC-3：项目内唯一，占用者 episode_id={taken.episode_id}）"
                        ),
                    ) from exc
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="场次写入冲突"
            ) from exc

        self._outbox_writer.record(
            aggregate_type="Scene",
            aggregate_id=scene.entity_id,
            event_type="SCENE_CREATED",
            tenant_id=tenant_id,
            payload={
                "scene_id": scene.entity_id,
                "project_id": project_id,
                "episode_id": episode.entity_id,
                "scene_code": scene_code,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
                "status": scene.status.value,
            },
        )
        return scene

    async def _resolve_parents(
        self,
        project_id: str,
        episode_id: str | None,
        episode_ref: str | None,
    ) -> tuple[Project | None, Episode, UUID | None]:
        """锁定并解析父链，返回 (project | None, episode, 组织归属键 | None)。

        - governance 线（``episode_ref``）：必须装配 project_repository，按
          Project → Episode 固定锁序带锁读，返回 project 以触发服务层的组织键与
          逐级冻结判定。父行查不到必须失败关闭——RLS 在租户上下文不符时会静默
          返回 0 行，写成 ``if parent is not None`` 会让越权写入被包装成「父不存在」。
        - A13 正式链（``episode_id``）：只按 id 读集数并校验归属；父链冻结与组织
          隔离由数据库守卫/RLS 承担（见 ADR-R40），服务层不重复判定，故 project
          返回 None、组织键返回 None。
        """
        if episode_ref is not None:
            project = await self._project_repo().get_by_id_for_child_write(project_id)
            if project is None:
                raise DomainError(
                    ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}"
                )
            episode = await self._episode_repository.get_by_project_ref_for_child_write(
                project_id, episode_ref
            )
            if episode is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"集数 {episode_ref} 不属于项目 {project_id}",
                )
            return project, episode, project.organization_id

        # episode_id 寻址：A13 正式链口径，只校验集数归属；冻结交给 DB 守卫。
        episode = await self._episode_repository.get_by_id(episode_id or "")
        if episode is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"集数不存在: {episode_id}"
            )
        if episode.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="集数不属于声明的项目（跨项目挂载被拒绝）",
            )
        return None, episode, None

    def _project_repo(self) -> ProjectRepository:
        """返回父项目仓储；未装配则 fail-closed——治理线父链冻结不能静默缺席。"""
        if self._project_repository is None:
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail="SceneService 缺少 project_repository，父链冻结守卫不可用",
            )
        return self._project_repository

    @staticmethod
    def _assert_ancestors_open_for_child_write(
        project: Project, episode: Episode
    ) -> None:
        """逐级判定父链未冻结——不是只判直接父。

        Project 归档后其下 Episode 仍可能是 ACTIVE（历史数据或先归档上层的
        时序），只判 Episode 会让「上层已封存、下层还在长」悄悄成立。判定从
        生命周期对象派生，与 SCRIPT.IMPORT / EPISODE.CREATE 共用同一规则。
        """
        PROJECT_LIFECYCLE.assert_child_creation_allowed(
            project.status, child_object="Scene"
        )
        EPISODE_LIFECYCLE.assert_child_creation_allowed(
            episode.status, child_object="Scene"
        )

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

        # SC-5 归档前置条件：何时求值由边表派生，不写死 `if target is ARCHIVED`
        # ——进入归档的边有两条（DRAFT/ACTIVE），手写分支只覆盖想到的那条。
        child_precondition: ChildPrecondition | None = SCENE_LIFECYCLE.child_precondition(
            scene.status, target_status
        )
        if child_precondition is not None:
            if self._shot_repository is None:
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR,
                    detail="SceneService 缺少 shot_repository，SC-5 归档守卫不可用",
                )
            scene = await self._lock_and_assert_no_shot_in_production(
                scene, child_precondition
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
            organization_id=scene.organization_id,
        )
        return scene

    async def _lock_and_assert_no_shot_in_production(
        self, scene: Scene, precondition: ChildPrecondition
    ) -> Scene:
        """持排他行锁重读场次，并在锁内判定「其下无在产镜头」。

        锁把两侧从「各看各的快照」变成「同一行上的先后」：归档持排他锁重判，
        镜头进入在产前先取同一行的共享锁（ShotService）。任一方先到，另一方
        都要等它提交，再基于它的结果重新判定。返回**锁内读到的那一行**——
        判定与版本必须同源，否则乐观锁用的是过期版本，正常归档会被误报冲突。
        """
        shots = self._shot_repository
        if shots is None:  # 调用点已判；此处再收窄让类型系统也能证明守卫存在
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail="SceneService 缺少 shot_repository，SC-5 归档守卫不可用",
            )
        locked = await self._repository.lock_for_archive(scene.entity_id)
        if locked is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {scene.entity_id}"
            )
        if await shots.has_with_status(
            locked.entity_id, ShotStatus(precondition.forbidden_status)
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED, detail=precondition.message
            )
        return locked


def _assert_same_request(
    existing: Scene, project_id: str, episode_id: str, scene_code: str
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。

    只比「键」不比「状态」：状态是这次写入的**结果**，不是请求的一部分。
    """
    if (
        existing.project_id != project_id
        or existing.episode_id != episode_id
        or existing.scene_code != scene_code
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
