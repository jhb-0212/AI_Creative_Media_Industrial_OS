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

from sqlalchemy.exc import IntegrityError

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
        - title / project_id / episode_ref / created_by 均非空。
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

        return script

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
        latest.transition_to(
            ScriptStatus.SUPERSEDED, audit_ref=f"superseded_by:{new_entity_id}"
        )
        await self._repository.save(latest)

    async def get_script(self, entity_id: str) -> ScriptRevision:
        """查询剧本版本，不存在抛 NOT_FOUND。"""
        script = await self._repository.get_by_id(entity_id)
        if script is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"剧本版本不存在: {entity_id}")
        return script
