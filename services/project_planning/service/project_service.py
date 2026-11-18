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
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.service.transition import advance_status


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
        created_by: str,
        tenant_id: UUID,
        entity_id: str | None = None,
        organization_id: UUID | None = None,
    ) -> Project:
        """创建项目（幂等：重复 entity_id 返回既有项目）。

        契约要求：
        - project_id 使用 UUID，不复用（8.2）。
        - project_name、created_by 非空。
        - tenant_id 来自可信身份上下文（Principal），标识项目所属租户，
          是 RLS 行级隔离键（决策 5）。
        - organization_id 可选（F04）：非空时项目归属指定组织；空=个人租户。
          组织归属的真实性由数据库外键 fk_projects_organization 兜底。
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
                _assert_same_request(
                    existing, project_name, project_tier, organization_id
                )
                return existing

        project = Project(
            entity_id=entity_id or uuid.uuid4().hex,
            project_name=project_name,
            project_tier=project_tier,
            created_by=created_by,
            tenant_id=tenant_id,
            organization_id=organization_id,
        )

        # 用 INSERT 语义新增；并发下唯一约束兜底 check-then-act 竞态：
        # 冲突时降级为幂等返回既有记录，而非报错或重复插入。
        try:
            await self._repository.add(project)
        except ReferentialIntegrityError as exc:
            # 引用/取值不成立（不是"重复提交"）：例如组织被并发删除、值违反 CHECK。
            # 这类失败不可幂等——降级成"返回既有记录"会把真实的一致性问题藏起来。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="项目写入引用的实体已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            if entity_id is None:
                # 无显式 id 时 UUID 冲突概率可忽略，仍转结构化内部错误。
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR, detail="项目写入冲突"
                ) from exc
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(
                    existing, project_name, project_tier, organization_id
                )
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
            tenant_id=tenant_id,
            payload={
                "project_id": project.entity_id,
                "project_name": project.project_name,
                "project_tier": project.project_tier.value,
                "created_by": project.created_by,
                "tenant_id": str(tenant_id),
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

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: ProjectStatus,
        changed_by: str,
        tenant_id: UUID,
    ) -> Project:
        """推进项目状态（PROJECT.STATUS.CHANGE）。

        契约要求：
        - 目标状态作参数，转移合法性由 domain/state_machine.py 的单一权威
          转移表校验（未登记转移一律拒绝，31.5）。
        - 终态 ARCHIVED 不可原地复活（红线）。
        - 乐观锁：读取时捕获 entity_version，条件更新；并发冲突抛
          ConcurrentModificationError。
        - 状态变更与 PROJECT_STATUS_CHANGED 事件同事务写入（R07/P2-1）。
        """
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )

        project = await self._repository.get_by_id(entity_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"项目不存在: {entity_id}"
            )

        # 唯一推进路径（ADR-R23 §D2-b）：守卫求值 → 乐观锁落库 → 写事件
        # 全部由 transition.advance_status 承载，本处不再复制这四步。
        await advance_status(
            project,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="Project",
            event_type="PROJECT_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={"project_id": project.entity_id},
            resource_type="Project",
            owner_action="PROJECT.STATUS.CHANGE",
            conflict_detail="项目状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
        )
        return project


def _assert_same_request(
    existing: Project,
    project_name: str,
    project_tier: ProjectTier,
    organization_id: UUID | None,
) -> None:
    """幂等键复用必须是同一请求：内容不一致一律 409。

    比较字段必须覆盖「决定这条记录归属与语义」的全部输入，而不只是名字与
    等级。organization_id 是 F04 引入的归属键（非空=组织项目、空=个人租户），
    同一 entity_id 换一个组织再提交，是**另一个请求**——若不在幂等比较里，
    会被误判为「重复提交」而静默返回旧记录，调用方以为归属已变更（审查
    P2-01）。（tenant_id 由 RLS + 主键所在租户固定，不在请求差异面内。）
    """
    if (
        existing.project_name != project_name
        or existing.project_tier != project_tier
        or existing.organization_id != organization_id
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=(
                "同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求"
                f"（existing={existing.project_name}/{existing.project_tier.value}"
                f"/organization_id={existing.organization_id}）"
            ),
        )
