"""ShotDraft SQLAlchemy 仓储实现（数据所有权：shot_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import _orm_update_values, conditional_update
from services.shot_planning.domain.enums import ShotDraftSource, ShotDraftStatus
from services.shot_planning.domain.shot_draft import ShotDraft
from services.shot_planning.repository.orm import ShotDraftModel
from services.shot_planning.repository.shot_draft_repo import ShotDraftRepository


def _to_domain(model: ShotDraftModel) -> ShotDraft:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ShotDraftStatus(model.status)
        source = ShotDraftSource(model.source)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_drafts 存量状态非法: status={model.status!r} source={model.source!r}",
        ) from exc
    return ShotDraft(
        entity_id=model.entity_id,
        project_id=model.project_id,
        episode_ref=model.episode_ref,
        scene_ref=model.scene_ref,
        shot_code=model.shot_code,
        intent_summary=model.intent_summary,
        shot_description=model.shot_description,
        source=source,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(draft: ShotDraft) -> ShotDraftModel:
    """领域模型 -> ORM。"""
    return ShotDraftModel(
        entity_id=draft.entity_id,
        entity_type=draft.ENTITY_TYPE,
        schema_version=draft.schema_version,
        entity_version=draft.entity_version,
        status=draft.status.value,
        owner_module=draft.owner_module,
        tenant_id=draft.tenant_id,
        organization_id=draft.organization_id,
        created_at=draft.created_at,
        created_by=draft.created_by,
        updated_at=draft.updated_at,
        audit_ref=draft.audit_ref,
        project_id=draft.project_id,
        episode_ref=draft.episode_ref,
        scene_ref=draft.scene_ref,
        shot_code=draft.shot_code,
        intent_summary=draft.intent_summary,
        shot_description=draft.shot_description,
        source=draft.source.value,
    )


class SqlAlchemyShotDraftRepository(ShotDraftRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, draft: ShotDraft) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由唯一约束捕获（幂等兜底）。
        with translate_integrity_errors(entity=f"shot_draft_id={draft.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(draft))

    async def save(self, draft: ShotDraft, expected_version: int) -> None:
        # 乐观锁条件更新（语义同 EpisodeRepository.save）；仅登记变更，不 commit。
        await conditional_update(
            self._session,
            ShotDraftModel,
            draft.entity_id,
            expected_version,
            _orm_update_values(_to_orm(draft)),
        )

    async def get_by_id(self, entity_id: str) -> ShotDraft | None:
        stmt = select(ShotDraftModel).where(ShotDraftModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
