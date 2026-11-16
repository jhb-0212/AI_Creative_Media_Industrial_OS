"""Project 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「项目创建」用例。
- 幂等：同一 entity_id 重复创建返回既有对象，不产生第二条记录（D3 精神）；
  但幂等键只能复用「同一请求」——同 id 不同内容必须 409，不能静默返回旧数据
  （审计 P3-1：重放同 id 不同内容会得到"成功"但内容从未写入）。
- 创建与 PROJECT_CREATED 事件同事务原子写入（审计 P2-1：项目创建此前只落库
  不发事件，下游永远等不到项目生命周期起点）。
- 抛出结构化 DomainError，不泄漏内部实现细节。
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.identity.scope import assert_tenant_wide_scope
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository


class ProjectService:
    """项目创建与查询用例。"""

    def __init__(
        self, repository: ProjectRepository, outbox_writer: OutboxWriter
    ) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def create_project(
        self,
        *,
        project_name: str,
        project_tier: ProjectTier,
        principal: Principal,
        entity_id: str | None = None,
    ) -> Project:
        """创建项目（幂等：重复 entity_id 返回既有项目）。

        契约要求：
        - project_id 使用 UUID，不复用（8.2）。
        - project_name、created_by 非空。
        - 租户与组织归属来自可信身份上下文（Principal）：tenant 是 RLS 隔离键
          （决策 5），organization 是归属链的根（ADR-R17）。
        """
        project_name = project_name.strip()
        created_by = principal.subject_id.strip()

        # 新建归属根要求本组织内全部项目范围：新项目的 id 不在任何既有范围内，
        # 若允许项目级用户创建，写进去之后他自己也读不回来（自锁）；
        # 而放宽数据库 WITH CHECK 就等于允许"往看不见的项目里写数据"。
        assert_tenant_wide_scope(principal, "PROJECT.CREATE")

        if not project_name:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="project_name 不能为空",
            )
        if not created_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="created_by 不能为空",
            )

        # 快速路径：已存在同 id 直接返回（避免无谓的 INSERT）。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, project_name, project_tier)
                return existing

        project = Project(
            entity_id=entity_id or uuid.uuid4().hex,
            project_name=project_name,
            project_tier=project_tier,
            created_by=created_by,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )

        # 用 INSERT 语义新增；并发下唯一约束兜底 check-then-act 竞态：
        # 冲突时降级为幂等返回既有记录，而非报错或重复插入。
        try:
            await self._repository.add(project)
        except DuplicateEntityError as exc:
            if entity_id is None:
                # 无显式 id 时 UUID 冲突概率可忽略，仍转结构化内部错误。
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR, detail="项目写入冲突"
                ) from exc
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, project_name, project_tier)
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="项目写入冲突"
            ) from exc

        # 与创建同事务写 PROJECT_CREATED：项目生命周期起点必须可被下游订阅，
        # 否则 outbox 里项目只有"被读写"的痕迹，没有"诞生"的事实（P2-1）。
        self._outbox_writer.record(
            aggregate_type="Project",
            aggregate_id=project.entity_id,
            event_type="PROJECT_CREATED",
            tenant_id=principal.tenant_id,
            payload={
                "project_id": project.entity_id,
                "project_name": project.project_name,
                "project_tier": project.project_tier.value,
                "created_by": project.created_by,
                "tenant_id": str(principal.tenant_id),
                "status": project.status.value,
            },
        )

        return project

    async def get_project(self, entity_id: str) -> Project:
        """查询项目，不存在抛 NOT_FOUND。"""
        project = await self._repository.get_by_id(entity_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"项目不存在: {entity_id}",
            )
        return project


def _assert_same_request(
    existing: Project, project_name: str, project_tier: ProjectTier
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。"""
    if existing.project_name != project_name or existing.project_tier != project_tier:
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=(
                "同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求"
                f"（existing={existing.project_name}/{existing.project_tier.value}）"
            ),
        )
