"""Scene SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。

设计要点：
- 实现 SceneRepository 抽象接口，与内存实现可互换。
- 领域模型 <-> ORM 模型的双向映射集中在此，隔离持久化细节。
- 只经本仓储写 project_planning.scenes 表，其它模块禁止直写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import translate_write_error
from services.project_planning.domain.enums import SceneStatus
from services.project_planning.domain.scene import Scene
from services.project_planning.repository._versioning import (
    _orm_update_values,
    conditional_update,
)
from services.project_planning.repository.orm import SceneModel
from services.project_planning.repository.scene_repo import SceneRepository


def _to_domain(model: SceneModel) -> Scene:
    """ORM -> 领域模型。

    枚举反序列化做防御：存量数据若出现契约外的非法状态值，降级为结构化
    DomainError（数据一致性），而非裸 ValueError 导致 500。
    """
    try:
        status = SceneStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"scenes 存量状态非法: {model.status!r}",
        ) from exc
    return Scene(
        entity_id=model.entity_id,
        project_id=model.project_id,
        episode_id=model.episode_id,
        scene_code=model.scene_code,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        organization_id=model.organization_id,
    )


def _to_orm(scene: Scene) -> SceneModel:
    """领域模型 -> ORM。

    ``scene_id`` 取 ``scene.entity_id``：领域层没有第二个身份字段，ORM 列必须
    跟着同一个值，否则插入会撞 ``ck_scenes_single_identity``——那是数据库在
    替我们守「不许有两套标识」。
    """
    return SceneModel(
        entity_id=scene.entity_id,
        entity_type=scene.ENTITY_TYPE,
        schema_version=scene.schema_version,
        entity_version=scene.entity_version,
        status=scene.status.value,
        owner_module=scene.owner_module,
        tenant_id=scene.tenant_id,
        created_at=scene.created_at,
        created_by=scene.created_by,
        updated_at=scene.updated_at,
        audit_ref=scene.audit_ref,
        scene_id=scene.entity_id,
        scene_code=scene.scene_code,
        episode_id=scene.episode_id,
        project_id=scene.project_id,
        organization_id=scene.organization_id,
    )


class SqlAlchemySceneRepository(SceneRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, scene: Scene) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由唯一约束捕获（幂等兜底）。
        # 同时捕获 ProgrammingError：父对象不在当前组织作用域时 RLS 以 42501
        # 拒绝写入（ADR-R24），它与外键冲突对调用方是同一事实——引用不成立。
        try:
            async with self._session.begin_nested():
                self._session.add(_to_orm(scene))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=scene.entity_id)

    async def save(self, scene: Scene, expected_version: int) -> None:
        # 乐观锁条件更新（语义同 ScriptRepository.save）；仅登记变更，不 commit。
        await conditional_update(
            self._session,
            SceneModel,
            scene.entity_id,
            expected_version,
            _orm_update_values(_to_orm(scene)),
        )

    async def get_by_id(self, entity_id: str) -> Scene | None:
        stmt = select(SceneModel).where(SceneModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_by_project_code(
        self, project_id: str, scene_code: str
    ) -> Scene | None:
        stmt = select(SceneModel).where(
            SceneModel.project_id == project_id,
            SceneModel.scene_code == scene_code,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
