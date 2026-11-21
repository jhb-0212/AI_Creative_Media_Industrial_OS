"""ScriptRevision 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「剧本导入」（SCRIPT.IMPORT）、「锁定申请」
  （SCRIPT.LOCK.REQUEST）、「锁定批准」（SCRIPT.LOCK.APPROVE）三个用例。
- 归属验证：剧本必须挂到真实存在的项目 + 归属于该项目的集数，杜绝跨项目
  悬空引用（通过注入 ProjectRepository / EpisodeRepository 端口承载）。
- 幂等：同一 entity_id 重复导入返回既有版本。
- 新版本导入使旧版本转 SUPERSEDED（终态）。
- 身份从可信 Principal 解析（非客户端字符串），职责分离是运行时真实主体比对。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐（31.6 Action Registry）：
- SCRIPT.IMPORT：resource_type=ScriptRevision，scope=project/episode，
  MEDIUM 风险，Token=Y，导演/编剧/制片，检查项目写权限与导入来源。
- SCRIPT.LOCK.REQUEST：申请人提交锁定申请（DRAFT→LOCK_PENDING），
  requested_by = 当前 Principal（调用者本人，非客户端填字符串）。
- SCRIPT.LOCK.APPROVE：resource_type=ScriptLock，scope=project/episode，
  HIGH 风险，Token=Y，导演/授权编剧，批准人 = 当前 Principal，且必须
  与申请人（requested_by）不同（职责分离红线，Agent 不得自锁）。
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.identity.separation import require_different_subject
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    PROJECT_LIFECYCLE,
    SCRIPT_IMPORT_ACTION,
    SCRIPT_LOCK_APPROVE_ACTION,
    SCRIPT_LOCK_REQUEST_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.outbox_writer import OutboxWriter
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.transition import advance_status


class ScriptService:
    """剧本导入、锁定申请/批准与查询用例。"""

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
        principal: Principal,
        entity_id: str | None = None,
    ) -> ScriptRevision:
        """导入剧本版本（幂等，产出 DRAFT 态可编辑版本）。

        契约要求：
        - script_revision_id 使用 UUID，不复用（8.2）。
        - title / project_id / episode_ref 均非空。
        - created_by 来自 Principal（可信身份），tenant_id 亦来自 Principal。
        - 归属验证：project 存在，且 episode 归属该 project。
        - 新版本导入使旧版本转 SUPERSEDED。

        输入归一化：所有字符串字段在方法入口统一 strip，后续查询与
        入库使用同一份归一化值，消除「查询带空格查不到旧版本」的口径
        不一致问题（见 M2）。
        """
        project_id = project_id.strip()
        episode_ref = episode_ref.strip()
        title = title.strip()
        created_by = principal.subject_id.strip()

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

        # 归属验证：project 存在 + episode 归属该项目（P1#6），并判定父集数未冻结。
        # 这是**创建**路径，故用带锁读；审批推进两处仍走无锁版（见方法文档）。
        await self._assert_episode_open_for_child_write(project_id, episode_ref)

        # 幂等：若已存在同 id，直接返回——但只允许"同一请求"的复用。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(
                    existing, project_id=project_id, episode_ref=episode_ref, title=title
                )
                return existing

        # 新版本的实体 id 在此处确定（无显式 id 时生成 UUID），
        # 使 audit_ref 能引用真实的新版本 id，避免拼出 "superseded_by:None"。
        new_entity_id = entity_id or uuid.uuid4().hex

        # 新版本导入：使「同一项目 + 同一集数」下当前未取代的旧版本转 SUPERSEDED。
        # 版本链按 project_id + episode_ref 隔离，避免跨集误替换。
        await self._supersede_active_version(
            project_id,
            episode_ref,
            new_entity_id,
            principal.tenant_id,
            actor=principal.subject_id,
        )

        script = ScriptRevision(
            entity_id=new_entity_id,
            project_id=project_id,
            episode_ref=episode_ref,
            title=title,
            created_by=created_by,
            tenant_id=principal.tenant_id,
        )

        # INSERT 语义新增；并发下唯一约束兜底 check-then-act 竞态。
        try:
            await self._repository.add(script)
        except ReferentialIntegrityError as exc:
            # 归属项目/集数在并发窗口里被删除，或取值违反 CHECK：引用不成立。
            # 与「同集重复导入」是两回事——后者由 ux_script_active_version 触发，
            # 属唯一冲突（下面 DuplicateEntityError 分支），可幂等/可重试。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="剧本归属的项目或集数已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            if entity_id is None:
                # 无显式 id 的冲突来自 ux_script_active_version（并发导入同集）：
                # 这不是内部错误，而是可重试的并发冲突，让客户端基于最新版本重试。
                raise DomainError(
                    ErrorRegistry.CONCURRENT_MODIFICATION,
                    detail="剧本版本并发冲突，请基于最新版本重试",
                ) from exc
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(
                    existing, project_id=project_id, episode_ref=episode_ref, title=title
                )
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="剧本版本写入冲突"
            ) from exc

        # 审计事件（决策 3）：剧本导入与状态落库同事务原子记录，不可变。
        self._outbox_writer.record(
            aggregate_type="ScriptRevision",
            aggregate_id=script.entity_id,
            event_type="SCRIPT_IMPORTED",
            tenant_id=principal.tenant_id,
            payload={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "title": title,
                "created_by": created_by,
                "tenant_id": str(principal.tenant_id),
                "status": ScriptStatus.DRAFT.value,
            },
        )
        return script

    async def request_lock(
        self,
        *,
        entity_id: str,
        principal: Principal,
    ) -> ScriptRevision:
        """提交锁定申请（SCRIPT.LOCK.REQUEST，DRAFT→LOCK_PENDING）。

        两步审批流（职责分离）第一步：申请人 = 当前 Principal（调用者本人），
        写入 requested_by，供批准动作做「批准人 != 申请人」的运行时比对。
        身份来自可信 Principal，非客户端字符串（根治 P0-3 身份来源不可信）。
        """
        requested_by = principal.subject_id.strip()
        if not requested_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="requested_by 不能为空"
            )

        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}"
            )

        # 归属验证（申请前要求归属合法）。
        await self._assert_episode_ownership(script.project_id, script.episode_ref)

        # 唯一推进路径（ADR-R23 §D2-b）：状态、申请人、事实行、事件同一序列。
        # domain_operation 承载「顺便写申请人」——领域方法内部仍经 transition_to
        # 改状态，聚合上没有第二条改状态的路径。
        await advance_status(
            script,
            ScriptStatus.LOCK_PENDING,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ScriptRevision",
            event_type="SCRIPT_LOCK_REQUESTED",
            tenant_id=principal.tenant_id,
            payload={
                "project_id": script.project_id,
                "episode_ref": script.episode_ref,
                "requested_by": requested_by,
            },
            resource_type="ScriptRevision",
            owner_action=SCRIPT_LOCK_REQUEST_ACTION,
            conflict_detail="剧本锁定申请并发冲突，请基于最新状态重试",
            actor=requested_by,
            domain_operation=lambda ref, act: script.request_lock(requested_by, ref, act),
        )
        return script

    async def approve_lock(
        self,
        *,
        entity_id: str,
        principal: Principal,
    ) -> ScriptRevision:
        """批准锁定（SCRIPT.LOCK.APPROVE，LOCK_PENDING→LOCKED）。

        两步审批流（职责分离）第二步：批准人 = 当前 Principal（调用者本人），
        必须与申请人（requested_by，申请时落库的真实主体）不同——职责分离
        红线在运行时以真实主体比对判定，Agent 不得自锁。
        """
        approved_by = principal.subject_id.strip()
        if not approved_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="approved_by 不能为空"
            )

        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}"
            )

        # 归属验证（批准前要求归属合法）。
        await self._assert_episode_ownership(script.project_id, script.episode_ref)

        # 红线：职责分离 fail-closed。申请人（requested_by）与批准人（当前主体）
        # 必须是两个不同真实主体；缺少申请人说明审批链不完整，同样拒绝。
        # 统一判定见 packages/identity/separation.py（消除重复比对逻辑）。
        require_different_subject(
            actor=approved_by,
            counterpart=script.requested_by,
            action_label="SCRIPT.LOCK.APPROVE",
            actor_label="批准人",
            counterpart_label="申请人",
        )

        # 唯一推进路径（ADR-R23 §D2-b）。批准人落在两处第一类事实里：事件的
        # approved_by 与状态迁移事实的 actor_subject——「批准」这个词必须能
        # 回答「谁批的」，且不依赖 outbox 保留期。
        await advance_status(
            script,
            ScriptStatus.LOCKED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ScriptRevision",
            event_type="SCRIPT_LOCK_APPROVED",
            tenant_id=principal.tenant_id,
            payload={
                "project_id": script.project_id,
                "episode_ref": script.episode_ref,
                "requested_by": script.requested_by,
                "approved_by": approved_by,
            },
            resource_type="ScriptRevision",
            owner_action=SCRIPT_LOCK_APPROVE_ACTION,
            conflict_detail="剧本锁定批准并发冲突，请基于最新状态重试",
            actor=approved_by,
            domain_operation=lambda ref, act: script.approve_lock(approved_by, ref, act),
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

    async def _assert_episode_open_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode:
        """创建剧本版本前的父链校验：归属 + 行锁 + **整条父链**的冻结判定。

        与 ``_assert_episode_ownership`` 的差别，每一点都有原因：

        1. 用**带锁**读（``FOR SHARE``）：否则「读到父状态 → 判定冻结 → 插入
           子行」之间父行可被并发归档，检查在写入瞬间已不成立。实测见
           ``docs/audits/mig-recon/verify_parent_freeze_lock.py``（H2/H4）与
           ``verify_parent_freeze_e2e.py``（R1~R3，走真实 SQLAlchemy 事务）。
        2. 判冻结：``*_LIFECYCLE.assert_child_creation_allowed``。冻结态记在状态上
           而不是动作上，新增创建路径自动继承，漏写不会静默放行。
        3. **不塞进** ``_assert_episode_ownership``：后者还被 ``LOCK.REQUEST`` /
           ``LOCK.APPROVE`` 两处审批推进复用，在那里加冻结会把「已归档集数下
           完成既有审批」误拒——那是对既有事实的补记，不是新建内容。

        **为什么必须一路查到 Project（2026-11-21 修正）**：原先只判直接父
        （Episode），于是在「Project 已归档、其下 Episode 仍为 ACTIVE」时导入
        照样成功——冻结只拦一层，上一层就被对称路径绕过（与只拦归档侧、
        不拦推进侧是同一形态的缺口）。父链只有三级且固定，逐级显式锁、显式判，
        比造一个「祖先遍历器」更容易看懂，也不会在链条变化时静默少查一层。

        **加锁顺序固定为自顶向下 Project → Episode → Scene**：所有创建路径都按
        这个次序取锁，因此并发创建之间不会形成环。反向取锁（先 Episode 后
        Project）与 ``EPISODE.CREATE``（只锁 Project）相遇即可能死锁。

        「父行查不到」必须拒绝，不能跳过判定：RLS 在租户上下文不符时会**静默
        返回 0 行而不报错**（同装置的 H9），此时若写成
        ``if parent is not None: 检查冻结``，锁与检查会一起消失。
        """
        # 第一级：Project（祖父）。行锁 + 冻结判定。
        project = await self._project_repository.get_by_id_for_child_write(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"项目不存在: {project_id}",
            )
        PROJECT_LIFECYCLE.assert_child_creation_allowed(
            project.status, child_object="ScriptRevision"
        )

        # 第二级：Episode（直接父）。行锁 + 归属 + 冻结判定。
        episode = await self._episode_repository.get_by_project_ref_for_child_write(
            project_id, episode_ref
        )
        if episode is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"集数 {episode_ref} 不属于项目 {project_id}",
            )
        EPISODE_LIFECYCLE.assert_child_creation_allowed(
            episode.status, child_object="ScriptRevision"
        )
        return episode

    async def _supersede_active_version(
        self,
        project_id: str,
        episode_ref: str,
        new_entity_id: str,
        tenant_id: uuid.UUID,
        *,
        actor: str,
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
        # 并发取代竞态由 advance_status 的 on_conflict="skip" 表达：若旧版本已被
        # 他人推进（版本号已变），返回 False 并幂等跳过——终态已达成，不再二次
        # transition，也不对终态报错。该策略是剧本取代独有的，故显式声明而非默认。
        advanced = await advance_status(
            latest,
            ScriptStatus.SUPERSEDED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ScriptRevision",
            event_type="SCRIPT_SUPERSEDED",
            tenant_id=tenant_id,
            payload={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "superseded_by": new_entity_id,
            },
            resource_type="ScriptRevision",
            # 新版本导入使旧版本 SUPERSEDED（state_machines.yaml#ScriptRevision 的 note）：
            # 拥有这条边的是 SCRIPT.IMPORT，而不是某个"取代"动作（契约里不存在该动作）。
            owner_action=SCRIPT_IMPORT_ACTION,
            conflict_detail="剧本版本取代并发冲突",
            actor=actor,
            on_conflict="skip",
        )
        if not advanced:
            return

    async def get_script(self, entity_id: str) -> ScriptRevision:
        """查询剧本版本，不存在抛 NOT_FOUND。"""
        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}")
        return script


def _assert_same_request(
    existing: ScriptRevision, *, project_id: str, episode_ref: str, title: str
) -> None:
    """幂等键复用必须是同一请求（P3-1）。

    此前同 entity_id 命中即返回旧对象：调用方拿着"新标题"拿到 201，
    库里却还是旧标题——数据从未写入却报成功，是最坏的一种谎。
    """
    if (
        existing.project_id != project_id
        or existing.episode_ref != episode_ref
        or existing.title != title
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=(
                "同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求"
            ),
        )
