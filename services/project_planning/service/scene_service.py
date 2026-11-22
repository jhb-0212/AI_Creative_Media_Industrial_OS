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
        shot_repository: ShotRepository,
        project_repository: ProjectRepository,
    ) -> None:
        """两条守卫依赖（镜头、父项目）都是**必需**的。

        它们曾经是可选的，靠调用点 ``if xxx is None: raise`` 兜底。那种写法防不住
        真正的失效模式：生产装配漏传时，构造照常成功，直到某次归档或某次登记才
        在运行期 500。而漏传的那个分支恰好是「判定守卫」——它不会拦错数据，它
        会让正确的操作失败，或者更糟：在写对 ``if`` 之前静默跳过判定。

        守卫依赖的可用性因此不该是运行期检查，而是类型系统的前提：
        **构造成功 ⇒ 两个不变量都有执行者**。
        """
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

        # 组织键只有治理线（episode_ref 寻址、走归属链表）要求。
        # A13 正式链的组织归属由父项目直接决定，个人租户项目为 NULL 是合法的；
        # 在这里无条件判空，会把合法的个人租户项目挡在门外。
        if episode_ref is not None and organization_id is None:
            # 场次的组织归属键在归属链表上是 NOT NULL，而个人租户项目没有组织键。
            # 给出可诊断的 4xx，不让它落到 23502 被翻译成「归属已变化」。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    "项目缺少组织归属，无法登记场次："
                    "场次表要求 (project_id, tenant_id, organization_id) 三列成立"
                ),
            )

        # 父链冻结：**两条寻址线共有**，且必须在幂等检查之后（见上）。
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
    ) -> tuple[Project, Episode, UUID | None]:
        """锁定并解析父链，返回 (project, episode, 组织归属键 | None)。

        两条寻址线**都**走「Project → Episode」固定锁序带锁读，差别只在组织键：

        - governance 线（``episode_ref``，走归属链表）：返回
          ``project.organization_id``，由调用点判非空。
        - A13 正式链（``episode_id``）：组织归属由父项目直接决定，个人租户项目
          为 NULL 是合法的，故返回 None、调用点不判空。

        为什么正式链也必须锁读父链
        ---------------------------------
        此前这里写的是「父链冻结与组织隔离由数据库守卫/RLS 承担」，而库里既没有
        触发器、也没有带状态谓词的 RLS 策略——那句话没有对应物。它造成的实际效果
        是：``episode_id`` 寻址下归档项目/集数仍可新增场次，而所有闸门都是绿的
        （因为缺口的方向恰好是不安全的那一侧）。规则现在只有一份实现，两条线都走它。

        为什么正式链要两跳读
        ---------------------------------
        本端口只有按 ``(project_id, episode_ref)`` 的带锁读，而入参是 ``entity_id``。
        先按 id 读到业务键、再按业务键带锁读权威行：``episode_ref`` 是创建时固定的
        标识，领域对象上没有任何修改它的路径，因此二次定位不会漂移。两跳之间若被
        并发归档，锁读读到的是新版本（READ COMMITTED 重检），判定随之成立。

        父行查不到必须失败关闭——RLS 在租户上下文不符时会静默返回 0 行，写成
        ``if parent is not None`` 会让越权写入被包装成「父不存在」。
        """
        project = await self._project_repository.get_by_id_for_child_write(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {project_id}"
            )

        if episode_ref is not None:
            episode = await self._episode_repository.get_by_project_ref_for_child_write(
                project_id, episode_ref
            )
            if episode is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"集数 {episode_ref} 不属于项目 {project_id}",
                )
            return project, episode, project.organization_id

        # episode_id 寻址：先定位业务键，再带锁读权威行。
        probe = await self._episode_repository.get_by_id(episode_id or "")
        if probe is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"集数不存在: {episode_id}"
            )
        if probe.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="集数不属于声明的项目（跨项目挂载被拒绝）",
            )
        episode = await self._episode_repository.get_by_project_ref_for_child_write(
            project_id, probe.episode_ref
        )
        if episode is None:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="集数在读取与锁定之间被变更，请重试",
            )
        return project, episode, None

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
