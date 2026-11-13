"""ScriptRevision 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「剧本导入」（SCRIPT.IMPORT）与「锁定审批」
  （SCRIPT.LOCK.APPROVE）两个用例。
- 归属验证：剧本必须挂到真实存在的项目 + 归属于该项目的集数，杜绝跨项目
  悬空引用（通过注入 ProjectRepository / EpisodeRepository 端口承载）。
- 幂等：同一 entity_id 重复导入返回既有版本。
- 新版本导入使旧版本转 SUPERSEDED（终态）。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐（31.6 Action Registry）：
- SCRIPT.IMPORT：resource_type=ScriptRevision，scope=project/episode，
  MEDIUM 风险，Token=Y，导演/编剧/制片，检查项目写权限与导入来源。
- SCRIPT.LOCK.APPROVE：resource_type=ScriptLock，scope=project/episode，
  HIGH 风险，Token=Y，导演/授权编剧，申请人与批准职责分离（Agent 不得自锁）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository._versioning import ConcurrentModificationError
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.outbox_writer import OutboxWriter
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository


class ScriptService:
    """剧本导入、锁定审批与查询用例。"""

    def __init__(
        self,
        repository: ScriptRepository,
        project_repository: ProjectRepository,
        episode_repository: EpisodeRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._project_repository = project_repository
        self._episode_repository = episode_repository
        self._outbox_writer = outbox_writer

    async def import_script(
        self,
        *,
        project_id: str,
        episode_ref: str,
        title: str,
        created_by: str,
        entity_id: str | None = None,
    ) -> ScriptRevision:
        """导入剧本版本（幂等，产出 DRAFT 态可编辑版本）。

        契约要求：
        - script_revision_id 使用 UUID，不复用（8.2）。
        - title / project_id / episode_ref / created_by 均非空。
        - 归属验证：project 存在，且 episode 归属该 project。
        - 新版本导入使旧版本转 SUPERSEDED。

        输入归一化：所有字符串字段在方法入口统一 strip，后续查询与
        入库使用同一份归一化值，消除「查询带空格查不到旧版本」的口径
        不一致问题（见 M2）。
        """
        project_id = project_id.strip()
        episode_ref = episode_ref.strip()
        title = title.strip()
        created_by = created_by.strip()

        if not title:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="title 不能为空")
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not episode_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="episode_ref 不能为空"
            )
        if not created_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="created_by 不能为空"
            )

        # 归属验证：project 存在 + episode 归属该项目（P1#6）。
        await self._assert_episode_ownership(project_id, episode_ref)

        # 幂等：若已存在同 id，直接返回。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                return existing

        # 新版本的实体 id 在此处确定（无显式 id 时生成 UUID），
        # 使 audit_ref 能引用真实的新版本 id，避免拼出 "superseded_by:None"。
        new_entity_id = entity_id or uuid.uuid4().hex

        # 新版本导入：使「同一项目 + 同一集数」下当前未取代的旧版本转 SUPERSEDED。
        # 版本链按 project_id + episode_ref 隔离，避免跨集误替换。
        await self._supersede_active_version(project_id, episode_ref, new_entity_id)

        script = ScriptRevision(
            entity_id=new_entity_id,
            project_id=project_id,
            episode_ref=episode_ref,
            title=title,
            created_by=created_by,
        )

        # INSERT 语义新增；并发下唯一约束兜底 check-then-act 竞态。
        try:
            await self._repository.add(script)
        except IntegrityError:
            if entity_id is None:
                raise
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                return existing
            raise

        # 审计事件（决策 3）：剧本导入与状态落库同事务原子记录，不可变。
        self._outbox_writer.record(
            aggregate_type="ScriptRevision",
            aggregate_id=script.entity_id,
            event_type="SCRIPT_IMPORTED",
            payload={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "title": title,
                "created_by": created_by,
                "status": ScriptStatus.DRAFT.value,
            },
        )
        return script

    async def lock_script(
        self,
        *,
        entity_id: str,
        requested_by: str,
        approved_by: str,
    ) -> ScriptRevision:
        """锁定剧本版本（SCRIPT.LOCK.APPROVE，DRAFT→LOCKED）。

        契约要求（31.6 SCRIPT.LOCK.APPROVE，HIGH 风险）：
        - 申请人与批准职责分离：requested_by != approved_by（Agent 不得自锁）。
        - 仅 DRAFT 态可锁定；终态/已锁定态拒绝。
        - 锁定需归属验证（剧本所属 project/episode 归属合法）。

        职责分离是红线，此处为服务层强制校验；运行时身份认证与 RBAC
        （第三阶段）落地前，先以显式参数区分申请人与批准人。
        """
        requested_by = requested_by.strip()
        approved_by = approved_by.strip()

        if not requested_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="requested_by 不能为空"
            )
        if not approved_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="approved_by 不能为空"
            )
        # 红线：申请人与批准职责分离，禁止自锁。
        if requested_by == approved_by:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="申请人与批准人不得为同一主体（职责分离红线）",
            )

        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}"
            )

        # 归属验证（锁定前同样要求归属合法）。
        await self._assert_episode_ownership(script.project_id, script.episode_ref)

        # 乐观锁：捕获读取时的版本号，作为条件更新的期望值。若在 transition
        # 与 save 之间被并发请求推进，save 会抛 ConcurrentModificationError，
        # 由调用方（路由层）转换为结构化 409 冲突，杜绝丢失更新。
        expected_version = script.entity_version

        # 状态机转移：仅 DRAFT→LOCKED（终态/已锁定由状态机拒绝）。
        script.transition_to(
            ScriptStatus.LOCKED,
            audit_ref=f"lock_approved_by:{approved_by}",
        )
        await self._repository.save(script, expected_version)

        # 审计事件（决策 3）：锁定审批与状态变更同事务原子记录，含职责分离
        # 证据（申请人与批准人），可追溯审批当时的完整上下文（R7）。
        self._outbox_writer.record(
            aggregate_type="ScriptRevision",
            aggregate_id=script.entity_id,
            event_type="SCRIPT_LOCK_APPROVED",
            payload={
                "project_id": script.project_id,
                "episode_ref": script.episode_ref,
                "requested_by": requested_by,
                "approved_by": approved_by,
                "from_status": ScriptStatus.DRAFT.value,
                "to_status": ScriptStatus.LOCKED.value,
            },
        )
        return script

    async def _assert_episode_ownership(
        self, project_id: str, episode_ref: str
    ) -> None:
        """归属验证：project 存在，且 episode 归属该项目。

        杜绝两类脏数据：
        - project_id 指向不存在的项目（悬空引用）。
        - episode_ref 不属于该项目（跨项目引用）。

        领域规则承载在仓储端口上：ProjectRepository 验证项目存在，
        EpisodeRepository 验证集数归属，不在此处拼 SQL。
        """
        project = await self._project_repository.get_by_id(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"项目不存在: {project_id}",
            )
        episode = await self._episode_repository.get_by_project_ref(
            project_id, episode_ref
        )
        if episode is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"集数 {episode_ref} 不属于项目 {project_id}",
            )

    async def _supersede_active_version(
        self, project_id: str, episode_ref: str, new_entity_id: str
    ) -> None:
        """将指定项目 + 集数下的当前活跃版本标记为 SUPERSEDED。

        并发安全设计：
        - 部分唯一索引（ux_script_active_version）保证同一 (project_id, episode_ref)
          最多一个活跃版本，数据库层兜底并发取代。
        - 若查询到的活跃版本已是 SUPERSEDED（被并发请求先行取代），幂等跳过，
          避免对终态二次 transition 抛 ValueError。
        - 仅当活跃版本与待插入的新版本不是同一条记录时才取代。
        """
        latest = await self._repository.get_latest_by_episode(project_id, episode_ref)
        if latest is None or latest.entity_id == new_entity_id:
            return
        if latest.status == ScriptStatus.SUPERSEDED:
            return
        # 乐观锁：捕获读取时版本，条件更新。并发下若该活跃版本已被另一请求
        # 先行取代（版本被推进），save 抛 ConcurrentModificationError——此时
        # 幂等跳过（终态已达成，无需二次 transition，也避免对终态报错）。
        expected_version = latest.entity_version
        latest.transition_to(
            ScriptStatus.SUPERSEDED, audit_ref=f"superseded_by:{new_entity_id}"
        )
        try:
            await self._repository.save(latest, expected_version)
        except ConcurrentModificationError:
            # 并发取代竞态：旧版本已被他人标记 SUPERSEDED，幂等跳过。
            return

        # 审计事件（决策 3）：版本取代同事务原子记录，保留取代链。
        self._outbox_writer.record(
            aggregate_type="ScriptRevision",
            aggregate_id=latest.entity_id,
            event_type="SCRIPT_SUPERSEDED",
            payload={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "superseded_by": new_entity_id,
                "from_status": latest.status.value,
                "to_status": ScriptStatus.SUPERSEDED.value,
            },
        )

    async def get_script(self, entity_id: str) -> ScriptRevision:
        """查询剧本版本，不存在抛 NOT_FOUND。"""
        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}")
        return script
