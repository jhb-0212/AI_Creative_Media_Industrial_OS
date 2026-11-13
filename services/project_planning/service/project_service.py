"""Project 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「项目创建」用例。
- 幂等：同一 entity_id 重复创建返回既有对象，不产生第二条记录（D3 精神）。
- 抛出结构化 DomainError，不泄漏内部实现细节。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository


class ProjectService:
    """项目创建与查询用例。"""

    def __init__(self, repository: ProjectRepository) -> None:
        self._repository = repository

    async def create_project(
        self,
        *,
        project_name: str,
        project_tier: ProjectTier,
        created_by: str,
        tenant_id: UUID,
        entity_id: str | None = None,
    ) -> Project:
        """创建项目（幂等：重复 entity_id 返回既有项目）。

        契约要求：
        - project_id 使用 UUID，不复用（8.2）。
        - project_name、created_by 非空。
        - tenant_id 来自可信身份上下文（Principal），标识项目所属租户，
          是 RLS 行级隔离键（决策 5）。
        """
        project_name = project_name.strip()
        created_by = created_by.strip()

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
                return existing

        project = Project(
            entity_id=entity_id or uuid.uuid4().hex,
            project_name=project_name,
            project_tier=project_tier,
            created_by=created_by,
            tenant_id=tenant_id,
        )

        # 用 INSERT 语义新增；并发下唯一约束兜底 check-then-act 竞态：
        # 冲突时降级为幂等返回既有记录，而非报错或重复插入。
        try:
            await self._repository.add(project)
        except IntegrityError as exc:
            if entity_id is None:
                # 无显式 id 时 UUID 冲突概率可忽略，仍转结构化内部错误。
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR, detail="项目写入冲突"
                ) from exc
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="项目写入冲突"
            ) from exc

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
