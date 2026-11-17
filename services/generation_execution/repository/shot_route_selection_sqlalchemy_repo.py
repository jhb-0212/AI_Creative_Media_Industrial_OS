"""ShotRouteSelection SQLAlchemy 仓储实现（数据所有权：generation_execution 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from services.generation_execution.domain.enums import ShotRouteSelectionStatus
from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)
from services.generation_execution.repository.orm import ShotRouteSelectionModel
from services.generation_execution.repository.shot_route_selection_repo import (
    ShotRouteSelectionRepository,
)


def _to_domain(model: ShotRouteSelectionModel) -> ShotRouteSelection:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ShotRouteSelectionStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_route_selections 存量取值非法: status={model.status!r}",
        ) from exc
    return ShotRouteSelection(
        entity_id=model.entity_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        route_id=model.route_id,
        status=status,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(selection: ShotRouteSelection) -> ShotRouteSelectionModel:
    """领域模型 -> ORM。"""
    return ShotRouteSelectionModel(
        entity_id=selection.entity_id,
        entity_type=selection.ENTITY_TYPE,
        schema_version=selection.schema_version,
        entity_version=selection.entity_version,
        status=selection.status.value,
        owner_module=selection.owner_module,
        tenant_id=selection.tenant_id,
        organization_id=selection.organization_id,
        created_at=selection.created_at,
        created_by=selection.created_by,
        updated_at=selection.updated_at,
        audit_ref=selection.audit_ref,
        project_id=selection.project_id,
        shot_id=selection.shot_id,
        route_id=selection.route_id,
    )


class SqlAlchemyShotRouteSelectionRepository(ShotRouteSelectionRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, selection: ShotRouteSelection) -> None:
        """INSERT 语义 + SAVEPOINT：并发第二条路线由唯一约束捕获。

        SAVEPOINT 是必需的：唯一冲突会让整个事务进入 aborted 状态，调用方
        随后"回查既有生效路线"就会拿到 25P02——本该是"拒绝第二条"，结果
        变成"数据库连接坏了"。
        """
        with translate_integrity_errors(
            entity=f"shot_route_selection={selection.project_id}/{selection.shot_id}"
        ):
            async with self._session.begin_nested():
                self._session.add(_to_orm(selection))

    async def get_effective(
        self, *, project_id: str, shot_id: str
    ) -> ShotRouteSelection | None:
        stmt = select(ShotRouteSelectionModel).where(
            ShotRouteSelectionModel.project_id == project_id,
            ShotRouteSelectionModel.shot_id == shot_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
