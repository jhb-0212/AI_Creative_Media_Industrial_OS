"""生产路线与生成任务的 SQLAlchemy 仓储（R16，project_planning schema 过渡期）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.versioning import _orm_update_values, conditional_update
from services.production_planning.domain.artifact_manifest import ArtifactManifest
from services.production_planning.domain.generation_spec import GenerationSpec, ShotSpecFacts
from services.production_planning.domain.generation_task import GenerationTask
from services.production_planning.domain.production_route import ProductionRoute
from services.production_planning.repository.orm import (
    ArtifactManifestModel,
    GenerationSpecModel,
    GenerationTaskModel,
    ProductionRouteModel,
)
from services.project_planning.domain.enums import (
    ArtifactManifestStatus,
    GenerationTaskStatus,
    NoLifecycleStatus,
    ProductionRouteStatus,
)
from services.project_planning.repository.orm import ShotSpecModel


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
        generation_spec_id=task.generation_spec_id,
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
        generation_spec_id=model.generation_spec_id,
        status=status,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        asset_version_id=model.asset_version_id,
        quality_profile_version=model.quality_profile_version,
        organization_id=model.organization_id,
    )


def _spec_to_orm(spec: GenerationSpec) -> GenerationSpecModel:
    return GenerationSpecModel(
        entity_id=spec.entity_id,
        entity_type=spec.ENTITY_TYPE,
        schema_version=spec.schema_version,
        entity_version=spec.entity_version,
        status=spec.status.value,
        owner_module=spec.owner_module,
        tenant_id=spec.tenant_id,
        created_at=spec.created_at,
        created_by=spec.created_by,
        updated_at=spec.updated_at,
        audit_ref=spec.audit_ref,
        generation_spec_id=spec.generation_spec_id,
        project_id=spec.project_id,
        shot_revision_id=spec.shot_revision_id,
        shot_spec_id=spec.shot_spec_id,
        production_route_ref=spec.production_route_ref,
        constraint_snapshot_id=spec.constraint_snapshot_id,
        template_version=spec.template_version,
        derivation_version=spec.derivation_version,
        size_policy_id=spec.size_policy_id,
        size_policy_version=spec.size_policy_version,
        model_id=spec.model_id,
        provider=spec.provider,
        generation_parameters=spec.generation_parameters,
        prompt_text=spec.prompt_text,
        negative_prompt=spec.negative_prompt,
        media_type=spec.media_type,
        size=spec.size,
        aspect_ratio=spec.aspect_ratio,
        spec_sha256=spec.spec_sha256,
        prompt_sha256=spec.prompt_sha256,
    )


def _spec_to_domain(model: GenerationSpecModel) -> GenerationSpec:
    try:
        status = NoLifecycleStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"generation_specs 存量枚举非法: status={model.status!r}",
        ) from exc
    return GenerationSpec(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        generation_spec_id=model.generation_spec_id,
        project_id=model.project_id,
        shot_revision_id=model.shot_revision_id,
        shot_spec_id=model.shot_spec_id,
        production_route_ref=model.production_route_ref,
        constraint_snapshot_id=model.constraint_snapshot_id,
        template_version=model.template_version,
        derivation_version=model.derivation_version,
        size_policy_id=model.size_policy_id,
        size_policy_version=model.size_policy_version,
        model_id=model.model_id,
        provider=model.provider,
        generation_parameters=dict(model.generation_parameters or {}),
        prompt_text=model.prompt_text,
        negative_prompt=model.negative_prompt,
        media_type=model.media_type,
        size=model.size,
        aspect_ratio=model.aspect_ratio,
        spec_sha256=model.spec_sha256,
        prompt_sha256=model.prompt_sha256,
        status=status,
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

    # ---- GenerationSpec（ADR-R47）---------------------------------------- #

    async def add_spec(self, spec: GenerationSpec) -> GenerationSpec:
        self._session.add(_spec_to_orm(spec))
        await self._session.flush()
        return spec

    async def add_spec_reusing(self, spec: GenerationSpec) -> tuple[GenerationSpec, bool]:
        """插入规格；唯一冲突时按内容复用，内容不符则 409（S-4）。

        用 SAVEPOINT 而不是「先查再插」：并发两个请求可以同时查到「没有」再各自
        插入，只有唯一约束能真正裁决。失败后回滚到 savepoint 再重读，事务本身
        仍然可用——否则调用方拿到的是一个已被污染、连后续读都做不了的会话。
        """
        try:
            async with self._session.begin_nested():
                self._session.add(_spec_to_orm(spec))
                await self._session.flush()
            return spec, False
        except IntegrityError:
            existing = await self.get_spec(spec.generation_spec_id)
            if existing is None:
                # 唯一冲突却读不到：并发事务尚未提交，或冲突来自别的约束。
                # 不能假设「就是并发」，交调用方按幂等冲突重试。
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail="生成规格插入冲突但读不到既有行；请按同一幂等键重试",
                ) from None
            if (
                existing.spec_sha256 != spec.spec_sha256
                or existing.identity_payload() != spec.identity_payload()
            ):
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail=(
                        f"generation_spec_id={spec.generation_spec_id} 已存在但内容不同："
                        "同一 id 不得指向两份不同规格"
                    ),
                ) from None
            return existing, True

    async def get_spec(self, generation_spec_id: str) -> GenerationSpec | None:
        result = await self._session.execute(
            select(GenerationSpecModel).where(
                GenerationSpecModel.generation_spec_id == generation_spec_id
            )
        )
        model = result.scalar_one_or_none()
        return _spec_to_domain(model) if model is not None else None

    async def get_task_by_business_id(
        self, generation_task_id: str, *, tenant_id: object
    ) -> GenerationTask | None:
        """幂等键读取：业务任务号必须与租户一起比对，避免跨租户命中。"""
        result = await self._session.execute(
            select(GenerationTaskModel).where(
                GenerationTaskModel.generation_task_id == generation_task_id,
                GenerationTaskModel.tenant_id == tenant_id,
            )
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def get_shot_spec_for_revision(
        self,
        *,
        project_id: str,
        shot_revision_id: str,
        tenant_id: object,
        shot_spec_id: str = "",
    ) -> ShotSpecFacts | None:
        """跨域列级只读：取某修订的规格（是否 LOCKED 由调用方判定）。

        ``shot_specs`` 对同一修订至多一份（``uq_shot_spec_per_revision``），
        因此这里不需要「取最新」的排序策略——「最新」在存在唯一约束时是个
        伪问题。显式按 project/tenant 过滤，不依赖 RLS 单独兜底。
        """
        conditions = [
            ShotSpecModel.project_id == project_id,
            ShotSpecModel.shot_revision_id == shot_revision_id,
            ShotSpecModel.tenant_id == tenant_id,
        ]
        if shot_spec_id:
            conditions.append(ShotSpecModel.entity_id == shot_spec_id)
        result = await self._session.execute(select(ShotSpecModel).where(*conditions))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return ShotSpecFacts(
            entity_id=model.entity_id,
            project_id=model.project_id,
            shot_revision_id=model.shot_revision_id,
            aspect_ratio=model.aspect_ratio,
            camera_path=model.camera_path,
            subject_paths=model.subject_paths,
            shot_size_changes=model.shot_size_changes,
            locked_dimensions=model.locked_dimensions,
            status=model.status,
        )

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

    async def get_by_generation_task_id(self, generation_task_id: str) -> GenerationTask | None:
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

    async def list_pending_prepared(self, generation_task_id: str) -> list[ArtifactManifest]:
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
