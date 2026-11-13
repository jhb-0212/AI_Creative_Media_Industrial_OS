"""ScriptRevision SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository._versioning import (
    _orm_update_values,
    conditional_update,
)
from services.project_planning.repository.orm import ScriptModel
from services.project_planning.repository.script_repo import ScriptRepository


def _to_domain(model: ScriptModel) -> ScriptRevision:
    """ORM -> 领域模型。

    枚举反序列化做防御：存量数据中若出现契约外的非法状态值，
    降级为结构化 DomainError（数据一致性），而非裸 ValueError 导致 500。
    """
    try:
        status = ScriptStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"script_revisions 存量状态非法: {model.status!r}",
        ) from exc
    return ScriptRevision(
        entity_id=model.entity_id,
        project_id=model.project_id,
        episode_ref=model.episode_ref,
        title=model.title,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        requested_by=model.requested_by,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(script: ScriptRevision) -> ScriptModel:
    """领域模型 -> ORM。"""
    return ScriptModel(
        entity_id=script.entity_id,
        entity_type=script.ENTITY_TYPE,
        schema_version=script.schema_version,
        entity_version=script.entity_version,
        status=script.status.value,
        owner_module=script.owner_module,
        tenant_id=script.tenant_id,
        created_at=script.created_at,
        created_by=script.created_by,
        updated_at=script.updated_at,
        audit_ref=script.audit_ref,
        project_id=script.project_id,
        episode_ref=script.episode_ref,
        title=script.title,
        requested_by=script.requested_by,
    )


class SqlAlchemyScriptRepository(ScriptRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, script: ScriptRevision) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由唯一约束捕获（幂等兜底），
        # 失败仅回滚本次 INSERT，session 不进入 aborted 态。
        async with self._session.begin_nested():
            self._session.add(_to_orm(script))

    async def save(self, script: ScriptRevision, expected_version: int) -> None:
        # 乐观锁条件更新：WHERE entity_version = expected_version 原子匹配，
        # 命中 0 行抛 ConcurrentModificationError（丢失更新根治）。仅登记变更，
        # 不 commit：事务边界由请求层统一控制。
        await conditional_update(
            self._session,
            ScriptModel,
            script.entity_id,
            expected_version,
            _orm_update_values(_to_orm(script)),
        )

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        stmt = select(ScriptModel).where(ScriptModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        """查询指定项目 + 集数的最新未取代版本（DRAFT/LOCKED 态）。

        按 project_id + episode_ref 双键隔离版本链（见接口契约）。
        """
        stmt = (
            select(ScriptModel)
            .where(
                ScriptModel.project_id == project_id,
                ScriptModel.episode_ref == episode_ref,
                ScriptModel.status != ScriptStatus.SUPERSEDED.value,
            )
            .order_by(ScriptModel.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
