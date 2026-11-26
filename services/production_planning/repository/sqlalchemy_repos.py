"""生产路线与生成任务的 SQLAlchemy 仓储（R16，project_planning schema 过渡期）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.versioning import _orm_update_values, conditional_update
from services.production_planning.domain.artifact_manifest import ArtifactManifest
from services.production_planning.domain.generation_task import GenerationTask
from services.production_planning.domain.production_route import ProductionRoute
from services.production_planning.repository.orm import (
    ArtifactManifestModel,
    GenerationTaskModel,
    ProductionRouteModel,
)
from services.project_planning.domain.enums import (
    ArtifactManifestStatus,
    GenerationTaskStatus,
    ProductionRouteStatus,
)


def _route_to_orm(route: ProductionRoute) -> ProductionRouteModel:
    return ProductionRouteModel(
        entity_id=route.entity_id,
        entity_type=route.ENTITY_TYPE,
        schema_version=route.schema_version,
        entity_version=route.entity_version,
        status=route.status.value,
        owner_module=route.owner_module,
        tenant_id=route.tenant_id,
        created_at=route.created_at,
        created_by=route.created_by,
        updated_at=route.updated_at,
        audit_ref=route.audit_ref,
        route_id=route.route_id,
        project_id=route.project_id,
        shot_id=route.shot_id,
        task_ref=route.task_ref,
        route_type=route.route_type,
        capability_ref=route.capability_ref,
        basis_ref=route.basis_ref,
        quality_profile_version=route.quality_profile_version,
    )


def _route_to_domain(model: ProductionRouteModel) -> ProductionRoute:
    try:
        status = ProductionRouteStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"production_routes 存量枚举非法: status={model.status!r}",
        ) from exc
    return ProductionRoute(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        route_id=model.route_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        task_ref=model.task_ref,
        route_type=model.route_type,
        capability_ref=model.capability_ref,
        basis_ref=model.basis_ref,
        quality_profile_version=model.quality_profile_version,
        status=status,
    )


class SqlAlchemyProductionRouteRepository:
    """ProductionRoute 的持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, route: ProductionRoute) -> ProductionRoute:
        self._session.add(_route_to_orm(route))
        # flush 让 INSERT 先于后续乐观锁 UPDATE 落到事务里（autoflush=False，
        # 不显式 flush 时同事务内的 UPDATE 会命中 0 行而误判并发冲突）。
        await self._session.flush()
        return route

    async def save(self, route: ProductionRoute, expected_version: int) -> ProductionRoute:
        await conditional_update(
            self._session,
            ProductionRouteModel,
            route.entity_id,
            expected_version,
            _orm_update_values(_route_to_orm(route)),
        )
        return route

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        result = await self._session.execute(
            select(ProductionRouteModel).where(ProductionRouteModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _route_to_domain(model) if model is not None else None


def _task_to_orm(task: GenerationTask) -> GenerationTaskModel:
    return GenerationTaskModel(
        entity_id=task.entity_id,
        entity_type=task.ENTITY_TYPE,
        schema_version=task.schema_version,
        entity_version=task.entity_version,
        status=task.status.value,
        owner_module=task.owner_module,
        tenant_id=task.tenant_id,
        created_at=task.created_at,
        created_by=task.created_by,
        updated_at=task.updated_at,
        audit_ref=task.audit_ref,
        generation_task_id=task.generation_task_id,
        project_id=task.project_id,
        shot_id=task.shot_id,
        shot_revision_id=task.shot_revision_id,
        production_route_ref=task.production_route_ref,
        model_adapter_ref=task.model_adapter_ref,
        constraint_snapshot_id=task.constraint_snapshot_id,
        budget_ref=task.budget_ref,
        prompt_ref=task.prompt_ref,
        lease_owner=task.lease_owner,
        lease_expires_at=task.lease_expires_at,
        attempt_count=task.attempt_count,
        asset_version_id=task.asset_version_id,
        quality_profile_version=task.quality_profile_version,
        organization_id=task.organization_id,
    )


def _task_to_domain(model: GenerationTaskModel) -> GenerationTask:
    try:
        status = GenerationTaskStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"generation_tasks 存量枚举非法: status={model.status!r}",
        ) from exc
    return GenerationTask(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        generation_task_id=model.generation_task_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        shot_revision_id=model.shot_revision_id,
        production_route_ref=model.production_route_ref,
        model_adapter_ref=model.model_adapter_ref,
        constraint_snapshot_id=model.constraint_snapshot_id,
        budget_ref=model.budget_ref,
        prompt_ref=model.prompt_ref,
        status=status,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        asset_version_id=model.asset_version_id,
        quality_profile_version=model.quality_profile_version,
        organization_id=model.organization_id,
    )


def _manifest_to_orm(manifest: ArtifactManifest) -> ArtifactManifestModel:
    return ArtifactManifestModel(
        entity_id=manifest.entity_id,
        entity_type=manifest.ENTITY_TYPE,
        schema_version=manifest.schema_version,
        entity_version=manifest.entity_version,
        status=manifest.status.value,
        owner_module=manifest.owner_module,
        tenant_id=manifest.tenant_id,
        created_at=manifest.created_at,
        created_by=manifest.created_by,
        updated_at=manifest.updated_at,
        audit_ref=manifest.audit_ref,
        generation_task_id=manifest.generation_task_id,
        project_id=manifest.project_id,
        locator=manifest.locator,
        sha256=manifest.sha256,
        media_type=manifest.media_type,
        attempt_id=manifest.attempt_id,
    )


def _orm_to_manifest(model: ArtifactManifestModel) -> ArtifactManifest:
    return ArtifactManifest(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        status=ArtifactManifestStatus(model.status),
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        generation_task_id=model.generation_task_id,
        project_id=model.project_id,
        locator=model.locator,
        sha256=model.sha256,
        media_type=model.media_type,
        attempt_id=model.attempt_id,
    )


class SqlAlchemyGenerationTaskRepository:
    """GenerationTask 的持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_route_by_ref(
        self,
        route_ref: str,
        *,
        project_id: str,
        shot_id: str,
        tenant_id: object,
    ) -> ProductionRoute | None:
        """按 route_id 读取路线，并核对它确实归属本次生成的项目/镜头/租户。

        C4 冻结链的一环：任务创建必须证明 production_route_ref 指向一条属于同一
        项目/镜头/租户的 VERIFIED 路线。只按 route_id 读会让 A 项目的任务借用 B
        项目的路线（route_id 是业务短号，不是全局身份键），因此归属三列必须在此
        一并比对；状态过滤挡掉 REVOKED 路线。
        """
        result = await self._session.execute(
            select(ProductionRouteModel).where(
                ProductionRouteModel.route_id == route_ref,
                ProductionRouteModel.project_id == project_id,
                ProductionRouteModel.shot_id == shot_id,
                ProductionRouteModel.tenant_id == tenant_id,
                ProductionRouteModel.status == ProductionRouteStatus.VERIFIED.value,
            )
        )
        model = result.scalar_one_or_none()
        return _route_to_domain(model) if model is not None else None

    async def add(self, task: GenerationTask) -> GenerationTask:
        self._session.add(_task_to_orm(task))
        await self._session.flush()
        return task

    async def save(self, task: GenerationTask, expected_version: int) -> GenerationTask:
        await conditional_update(
            self._session,
            GenerationTaskModel,
            task.entity_id,
            expected_version,
            _orm_update_values(_task_to_orm(task)),
        )
        return task

    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        result = await self._session.execute(
            select(GenerationTaskModel).where(GenerationTaskModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def get_by_generation_task_id(
        self, generation_task_id: str
    ) -> GenerationTask | None:
        """按业务任务号读取（事件载荷使用业务号，不是随机 entity_id）。"""
        result = await self._session.execute(
            select(GenerationTaskModel).where(
                GenerationTaskModel.generation_task_id == generation_task_id
            )
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def next_claimable_for_claim(self) -> GenerationTask | None:
        """领取专用：FIFO 取最早 REQUESTED，并加 ``FOR UPDATE SKIP LOCKED``。

        为什么不复用普通 SELECT：只读查询下 N 个 Worker 会同时读到同一条最早任务，
        只有乐观锁成功的那一个真领到，其余在行锁上排队等它执行完——「并发」退化成
        「串行重试」。``SKIP LOCKED`` 让被别的 Worker 占住的行直接跳过，各自领各自
        的任务。

        锁只应覆盖「领取」这一步：调用方必须在**同一事务**内完成 claim 并立即提交，
        绝不能在提交前执行外部生成动作，否则锁会一直持有到任务结束。
        """
        result = await self._session.execute(
            select(GenerationTaskModel)
            .where(GenerationTaskModel.status == GenerationTaskStatus.REQUESTED.value)
            .order_by(GenerationTaskModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def list_expired(self, now: datetime) -> list[GenerationTask]:
        """租约已过期但仍停在 RUNNING 的任务（崩溃 Worker 留下的）。"""
        result = await self._session.execute(
            select(GenerationTaskModel).where(
                GenerationTaskModel.status == GenerationTaskStatus.RUNNING.value,
                GenerationTaskModel.lease_expires_at.is_not(None),
                GenerationTaskModel.lease_expires_at < now,
            )
        )
        return [_task_to_domain(model) for model in result.scalars()]

    async def list_succeeded_with_asset(self, limit: int = 50) -> list[GenerationTask]:
        """成功且登记了产物引用的任务（QC 漏入队对账的数据源）。

        只取带 ``asset_version_id`` 的行：没有产物引用的历史成功任务无法映射到
        待检资产，扫描它们只会制造无对象的入队请求。
        """
        result = await self._session.execute(
            select(GenerationTaskModel)
            .where(
                GenerationTaskModel.status == GenerationTaskStatus.SUCCEEDED.value,
                GenerationTaskModel.asset_version_id != "",
            )
            .order_by(GenerationTaskModel.updated_at.asc())
            .limit(limit)
        )
        return [_task_to_domain(model) for model in result.scalars()]

    # ---- ArtifactManifest（R42 T0/T1）------------------------------------ #

    async def add_manifest(self, manifest: ArtifactManifest) -> ArtifactManifest:
        self._session.add(_manifest_to_orm(manifest))
        await self._session.flush()
        return manifest

    async def get_manifest_by_task_attempt(
        self, generation_task_id: str, attempt_id: str
    ) -> ArtifactManifest | None:
        result = await self._session.execute(
            select(ArtifactManifestModel).where(
                ArtifactManifestModel.generation_task_id == generation_task_id,
                ArtifactManifestModel.attempt_id == attempt_id,
            )
        )
        model = result.scalar_one_or_none()
        return _orm_to_manifest(model) if model is not None else None

    async def list_pending_prepared(
        self, generation_task_id: str
    ) -> list[ArtifactManifest]:
        """PR-G：某任务所有未确认（PREPARED）的产物意图，按创建序稳定返回。

        恢复流程凭它决定「对象是否已落盘、要不要重新调用模型」。顺序显式
        ``ORDER BY created_at, entity_id``：调用方不得依赖数据库返回顺序（同一
        时刻多个 attempt 时，顺序决定先核对哪一个，必须可复现）。
        """
        result = await self._session.execute(
            select(ArtifactManifestModel)
            .where(
                ArtifactManifestModel.generation_task_id == generation_task_id,
                ArtifactManifestModel.status == ArtifactManifestStatus.PREPARED.value,
            )
            .order_by(
                ArtifactManifestModel.created_at.asc(),
                ArtifactManifestModel.entity_id.asc(),
            )
        )
        return [_orm_to_manifest(model) for model in result.scalars()]

    async def save_manifest(
        self, manifest: ArtifactManifest, expected_version: int
    ) -> ArtifactManifest:
        await conditional_update(
            self._session,
            ArtifactManifestModel,
            manifest.entity_id,
            expected_version,
            _orm_update_values(_manifest_to_orm(manifest)),
        )
        return manifest


__all__ = [
    "SqlAlchemyGenerationTaskRepository",
    "SqlAlchemyProductionRouteRepository",
]
