"""Scene 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「场次登记」（``SCENE.CREATE``）与「状态推进」
  （``SCENE.STATUS.CHANGE``）两个用例，以及查询。
- 归属验证：场次必须挂到真实存在的集，且该集属于请求里的项目——两级都用
  **带锁读**，因为父状态决定这次写入是否合法。
- 幂等：同一 entity_id 重复登记返回既有场次；内容不一致则 409。
- 登记与 ``SCENE_CREATED`` 事件同事务写入。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐（V205-R14-A13）：
- ``SCENE.CREATE``：scope=project/episode，MEDIUM，导演/编剧/制片。
- ``SCENE.STATUS.CHANGE``：目标状态作参数，运行时按 Scene 转移表校验。

为什么创建入口形状照抄 ``EPISODE.CREATE`` 而不是挂到 ``SHOT.REVISION.CONFIRM``
上：后者是 HIGH 风险、allowed_principals 为导演/镜头负责人。让「有镜头确认权
的人」顺带获得「建场次权」，等于把一个动作包了两级权限，而场次创建在业务上
属于拆镜/编剧范畴（与 ``EPISODE.CREATE`` 同一角色集）。ADR-R19 已先一步拒绝
过这个方向，本服务只是不把它偷偷做回来。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import SceneStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    PROJECT_LIFECYCLE,
    SCENE_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.scene_repo import SceneRepository
from services.project_planning.service.transition import advance_status


class SceneService:
    """场次登记、状态推进与查询用例。"""

    def __init__(
        self,
        repository: SceneRepository,
        episode_repository: EpisodeRepository,
        project_repository: ProjectRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._episode_repository = episode_repository
        self._project_repository = project_repository
        self._outbox_writer = outbox_writer

    async def create_scene(
        self,
        *,
        project_id: str,
        episode_ref: str,
        scene_code: str,
        created_by: str,
        tenant_id: UUID,
        entity_id: str | None = None,
    ) -> Scene:
        """登记场次（幂等，归属集与项目）。

        契约要求：
        - 场次必须归属真实存在的集，且该集属于请求的项目（``fk_scenes_episode_project``
          在数据库层再兜一次）。
        - 同项目内 ``scene_code`` 唯一（``uq_scenes_project_code``）。
        - ``scene_id`` 与 ``entity_id`` 同值（领域模型用 property 表达）。
        - ``organization_id`` 从父项目拷贝——调用方不得自填。

        刻意用 ``episode_ref`` 而不是 ``episode_id`` 定位父集：这是本域既有的
        寻址口径（``EPISODE.CREATE`` / ``SCRIPT.IMPORT`` 都用 project + ref），
        换一套只为「省一次映射」会让同一层级出现两种寻址方式。真实的
        ``episode_id`` 在父行读出后确定，写进 ``Scene.episode_id``。
        """
        project_id = project_id.strip()
        episode_ref = episode_ref.strip()
        scene_code = scene_code.strip()
        created_by = created_by.strip()

        for name, value in (
            ("project_id", project_id),
            ("episode_ref", episode_ref),
            ("scene_code", scene_code),
            ("created_by", created_by),
        ):
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        # 先锁父链并确认归属真实，再做幂等回读，最后才判父链冻结。
        # 三步的顺序不是排版问题：幂等必须先于冻结（否则归档后的重试会变成
        # 409，调用方永远无法确认首次是否成功），但锁必须先于幂等（否则判
        # 「父是否冻结」与插入之间仍有并发窗口）。
        project, episode = await self._lock_ancestors_for_child_write(
            project_id, episode_ref
        )

        # 幂等：已存在同 id 直接返回。
        # 刻意排在组织键与冻结判定**之前**：重放一个当时成功过的请求是重试
        # 而非新建，必须返回原结果；若先判冻结或组织键漂移，正常重试会变成
        # 4xx，调用方将永远无法确认自己那次创建到底有没有生效。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, project_id, episode.entity_id, scene_code)
                return existing

        if project.organization_id is None:
            # 场次的组织归属键是 NOT NULL，而个人租户的项目没有组织键。
            # 在这里给出可诊断的拒绝，而不是让它落到 23502 被翻译成
            # 「归属已变化」——后者会把一条明确的产品边界说成一次并发冲突。
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
            organization_id=project.organization_id,
        )

        # INSERT 语义新增；并发下唯一约束兜底（显式 id 与项目内编号两条路径）。
        try:
            await self._repository.add(scene)
        except ReferentialIntegrityError as exc:
            # 归属项目/集在并发窗口里被删除，或值违反 CHECK：引用不成立，不可幂等。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="场次归属的集或项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            existing = (
                await self._repository.get_by_id(entity_id)
                if entity_id is not None
                else await self._repository.get_by_project_code(project_id, scene_code)
            )
            if existing is not None:
                _assert_same_request(
                    existing, project_id, episode.entity_id, scene_code
                )
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="场次写入冲突"
            ) from exc

        # 与登记同事务写 SCENE_CREATED（场次诞生必须可被订阅）。
        self._outbox_writer.record(
            aggregate_type="Scene",
            aggregate_id=scene.entity_id,
            event_type="SCENE_CREATED",
            tenant_id=tenant_id,
            payload={
                "scene_id": scene.scene_id,
                "project_id": project_id,
                "episode_id": episode.entity_id,
                "scene_code": scene_code,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
                "status": scene.status.value,
            },
        )
        return scene

    async def _lock_ancestors_for_child_write(
        self, project_id: str, episode_ref: str
    ):
        """按自顶向下顺序锁定 Project → Episode，并确认归属真实。

        这里**只做加锁与存在性检查**，刻意不含冻结判定：冻结判定必须排在幂等
        回读之后，否则归档后的重试会被误拒。把两者拆开不是多一层抽象，而是
        让「先锁 → 再幂等 → 后判冻结」这个顺序在调用点可见，不再藏进一个名字
        含糊的函数里。

        加锁顺序固定为 ``Project → Episode → Scene``：所有创建路径都按这个次序
        取锁，并发创建之间不会成环。反过来先锁 Episode 再锁 Project，与
        ``EPISODE.CREATE``（只锁 Project）相遇即可能死锁。

        「父行查不到」必须拒绝，不能跳过：RLS 在租户上下文不符时会**静默返回
        0 行而不报错**，此时若写成 ``if parent is not None: ...``，锁与检查会
        一起消失，越权写入被包装成「父不存在」也只是次要风险。
        """
        project = await self._project_repository.get_by_id_for_child_write(project_id)
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
        return project, episode

    @staticmethod
    def _assert_ancestors_open_for_child_write(
        project: Project, episode: Episode
    ) -> None:
        """逐级判定父链未冻结——不是只判直接父。

        Project 归档后其下 Episode 仍可能是 ACTIVE（历史数据或先归档上层的
        时序），只判 Episode 会让「上层已封存、下层还在长」悄悄成立——这正是
        「只拦一半」的另一种形态，与「只拦归档侧不拦推进侧」同型。

        判定从生命周期对象派生，不在这里写状态字面量：同一条冻结规则还要
        服务于 SCRIPT.IMPORT 与 EPISODE.CREATE，写第三份必然腐化。
        """
        PROJECT_LIFECYCLE.assert_child_creation_allowed(
            project.status, child_object="Scene"
        )
        EPISODE_LIFECYCLE.assert_child_creation_allowed(
            episode.status, child_object="Scene"
        )

    async def get_scene(self, entity_id: str) -> Scene:
        """查询场次，不存在抛 NOT_FOUND。"""
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
        """推进场次状态（``SCENE.STATUS.CHANGE``）。

        契约要求：
        - 目标状态作参数，转移合法性由 ``SCENE_LIFECYCLE`` 单一权威校验（31.5）。
        - 终态 ARCHIVED 不可原地复活（边表里没有出边）。
        - 乐观锁：条件更新；并发冲突抛 ``CONCURRENT_MODIFICATION``。
        - 归档时「其下不得有在制镜头」不在这里判：那是跨表存在性不变量，
          由数据库守卫在 ``UPDATE`` 那一刻拒绝（ADR-R27/Q3）。服务层再查一遍
          只会得到「查的瞬间成立」的假保证，而且绕开服务层的写入不会被它拦住。
        """
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )

        scene = await self._repository.get_by_id(entity_id)
        if scene is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"场次不存在: {entity_id}")

        # 唯一推进路径（ADR-R23 §D2-b），与项目/集数/剧本共用同一序列。
        await advance_status(
            scene,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="Scene",
            event_type="SCENE_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "scene_id": scene.scene_id,
                "project_id": scene.project_id,
                "episode_id": scene.episode_id,
            },
            resource_type="Scene",
            owner_action=SCENE_STATUS_CHANGE_ACTION,
            conflict_detail="场次状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            organization_id=scene.organization_id,
        )
        return scene


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
            detail=(
                "同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求"
            ),
        )
