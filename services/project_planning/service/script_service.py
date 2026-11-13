"""ScriptRevision 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「剧本导入」用例（SCRIPT.IMPORT）。
- 幂等：同一 entity_id 重复导入返回既有版本。
- 新版本导入使旧版本转 SUPERSEDED（终态）。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐（31.6 SCRIPT.IMPORT）：
- resource_type=ScriptRevision，scope=project/episode，MEDIUM 风险，Token=Y。
- 检查项目写权限、文件安全与导入来源（Phase 1 先落地权限校验骨架，
  文件安全/导入来源校验在接入文件存储后补齐）。
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.script_repo import ScriptRepository


class ScriptService:
    """剧本导入与查询用例。"""

    def __init__(self, repository: ScriptRepository) -> None:
        self._repository = repository

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
        - title 非空；project_id 非空（归属校验）。
        - 新版本导入使旧版本转 SUPERSEDED。
        """
        if not title or not title.strip():
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="title 不能为空")
        if not project_id or not project_id.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )

        # 幂等：若已存在同 id，直接返回。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                return existing

        # 新版本导入：使当前未取代的旧版本转 SUPERSEDED。
        latest = await self._repository.get_latest_by_project(project_id)
        if latest is not None and latest.entity_id != entity_id:
            latest.transition_to(ScriptStatus.SUPERSEDED, audit_ref=f"superseded_by:{entity_id}")
            await self._repository.save(latest)

        script = ScriptRevision(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id.strip(),
            episode_ref=episode_ref.strip(),
            title=title.strip(),
            created_by=created_by,
        )
        await self._repository.save(script)
        return script

    async def get_script(self, entity_id: str) -> ScriptRevision:
        """查询剧本版本，不存在抛 NOT_FOUND。"""
        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}")
        return script
