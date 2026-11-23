"""GenerationTask 应用服务（生成请求 + 异步租约，R16-D3 / Phase 2 §1）。

请求与执行是两个事实，两个入口
------------------------------
``request_generation`` 只创建任务（停在 REQUESTED）；执行结果由持有租约的
执行器经 ``claim`` → ``finish`` 提交。请求方不能声明「成功了」——执行结果是
执行事实，不是请求参数。

为什么租约操作全部走本服务而不是端点里直接改字段
--------------------------------------------------
每一次租约变更（领取 / 续租 / 终结 / 到期回收）都必须同时留下状态事实与事件。
散在端点里写，任何一处漏发事件都不会被闸门发现；走 ``advance_status`` /
``save_versioned_entity`` 这两条唯一入口，写事件不是「记得写」，而是结构上无法跳过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.quality_profile import require_quality_profile
from packages.shared.storage_locator import parse_locator, validate_locator_or_raise
from services.production_planning.domain.artifact_manifest import ArtifactManifest
from services.production_planning.domain.generation_task import GenerationTask
from services.project_planning.domain.enums import (
    ArtifactManifestStatus,
    GenerationTaskStatus,
)
from services.project_planning.domain.state_machine import (
    ASSET_REGISTRATION_COMPLETED_EVENT,
    ASSET_REGISTRATION_REJECTED_EVENT,
    GENERATION_PROCESS_ACTION,
)
from services.project_planning.service.transition import advance_status, save_versioned_entity


def _now() -> datetime:
    return datetime.now(UTC)


class GenerationService:
    def __init__(self, repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def request_generation(
        self,
        *,
        project_id: str,
        shot_id: str,
        shot_revision_id: str,
        production_route_ref: str,
        model_adapter_ref: str,
        constraint_snapshot_id: str,
        budget_ref: str,
        prompt_ref: str,
        created_by: str,
        tenant_id: UUID,
        generation_task_id: str = "",
    ) -> GenerationTask:
        """创建生成任务，一律停在 REQUESTED，等 Worker 持租约领取。

        请求方不能声明执行结果
        ----------------------
        这里曾经接受 ``result`` 参数且默认 ``SUCCEEDED``：调用方不传就算成功，
        既没有真实产物也没有执行证据，却能把任务推进到终态。执行成功是执行事实，
        只能由持有租约的执行器经 ``claim`` / ``finish`` 提交——成功必须绑定真实
        产物版本与质量档案版本。

        创建不是状态迁移
        ----------------
        ``GenerationTask`` 的初始态就是 REQUESTED，创建没有 ``from_status``。
        ``advance_status`` 要求真实的状态边（同态会被生命周期守卫拒绝，事实表
        也会因 ``ck_status_transition_advances`` 违约），所以这里用 ``record``
        只登记事件：审计看得到「有人请求了生成」，事实表却不该凭空造一条迁移。

        质量策略冻结（ADR-R42 C4）
        --------------------------
        任务创建时按 ``production_route_ref`` 读取已 VERIFIED 的路线，核对它确实
        属于同一 project / shot / tenant，再从路线取出权威质量策略冻结进任务。
        此后 T1/T2/T3 与 QC 只读任务冻结值，绝不重新解析「当前版本」——路线可
        被撤销、契约可演进，但一个已受理的任务对自己受理时的质量口径负责。

        仓储方法在归属不符或路线已撤销时返回 None，这里统一转成 NOT_FOUND：
        不区分「路线不存在」与「路线不属于你」，避免泄露他人路线是否存在。
        """
        route = await self._repository.get_route_by_ref(
            production_route_ref,
            project_id=project_id,
            shot_id=shot_id,
            tenant_id=tenant_id,
        )
        if route is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=(
                    f"生成任务引用的生产路线不可用（route={production_route_ref}）："
                    "须为同一项目/镜头/租户下已 VERIFIED 的路线"
                ),
            )
        frozen_quality_profile = require_quality_profile(route.quality_profile_version)
        task = GenerationTask(
            entity_id=str(uuid4()),
            created_by=created_by,
            tenant_id=tenant_id,
            generation_task_id=generation_task_id,
            project_id=project_id,
            shot_id=shot_id,
            shot_revision_id=shot_revision_id,
            production_route_ref=production_route_ref,
            model_adapter_ref=model_adapter_ref,
            constraint_snapshot_id=constraint_snapshot_id,
            budget_ref=budget_ref,
            prompt_ref=prompt_ref,
            quality_profile_version=frozen_quality_profile,
        )
        await self._repository.add(task)
        self._outbox_writer.record(
            aggregate_type="GenerationTask",
            aggregate_id=task.entity_id,
            tenant_id=tenant_id,
            event_type="GENERATION_REQUESTED",
            payload={
                "generation_task_id": task.generation_task_id,
                "project_id": task.project_id,
                "shot_id": task.shot_id,
                "shot_revision_id": task.shot_revision_id,
                "model_adapter_ref": task.model_adapter_ref,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
            },
        )
        return task

    # ---- R42 T0/T1：写入意图与存储确认 ---------------------------------- #

    async def prepare_artifact(
        self,
        *,
        task_id: str,
        worker_id: str,
        locator: str,
        sha256: str,
        attempt_id: str,
    ) -> ArtifactManifest:
        """T0：落产物写入意图。此事务必须早于对象存储写入。"""
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        locator = validate_locator_or_raise(locator)
        parsed, problem = parse_locator(locator)
        if parsed is None:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=problem)
        digest = sha256.strip().lower()
        if parsed.sha256.lower() != digest:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="locator 内嵌摘要与产物 sha256 不一致，拒绝固化矛盾事实",
            )
        manifest = ArtifactManifest(
            entity_id=str(uuid4()),
            created_by=worker_id,
            tenant_id=task.tenant_id,
            generation_task_id=task.generation_task_id,
            project_id=task.project_id,
            locator=locator,
            sha256=digest,
            attempt_id=attempt_id,
        )
        await self._repository.add_manifest(manifest)
        return manifest

    async def confirm_artifact(
        self,
        *,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        locator: str,
        sha256: str,
        media_type: str,
    ) -> GenerationTask:
        """T1：确认对象已落盘，推 AWAITING 并把登记请求写入同一事务的 outbox。"""
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        manifest = await self._repository.get_manifest_by_task_attempt(
            task.generation_task_id, attempt_id
        )
        if manifest is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"产物写入意图不存在（task={task.generation_task_id}, attempt={attempt_id}）",
            )
        if manifest.status is not ArtifactManifestStatus.PREPARED:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"产物意图当前为 {manifest.status.value}，不能重复确认",
            )
        if manifest.locator != locator or manifest.sha256 != sha256.strip().lower():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="落盘产物的 locator/sha256 与 T0 意图不一致，禁止登记",
            )
        if not task.quality_profile_version.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="质量策略未冻结：任务缺少 quality_profile_version",
            )

        expected_version = manifest.entity_version
        await self._repository.save_manifest(
            manifest.mark_stored(f"t1-store:{uuid4()}"), expected_version
        )
        await advance_status(
            task,
            GenerationTaskStatus.AWAITING_ASSET_REGISTRATION,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="ASSET_REGISTRATION_REQUESTED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "project_id": task.project_id,
                "tenant_id": str(task.tenant_id),
                "locator": manifest.locator,
                "sha256": manifest.sha256,
                "attempt_id": manifest.attempt_id,
                "media_type": media_type,
                "quality_profile_version": task.quality_profile_version,
                "worker_id": worker_id,
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="任务已进入等待登记态，请勿重复确认",
            domain_operation=lambda audit_ref, action: task.await_asset_registration(
                worker_id, audit_ref, action
            ),
        )
        return task

    # ---- Phase 2：异步租约（GENERATION.PROCESS）-------------------------- #

    async def _load(self, task_id: str) -> GenerationTask:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"生成任务不存在: {task_id}"
            )
        return cast(GenerationTask, task)

    async def claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> GenerationTask:
        """REQUESTED → RUNNING：领取租约。"""
        task = await self._load(task_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await advance_status(
            task,
            GenerationTaskStatus.RUNNING,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_STARTED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count + 1,
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="任务已被其他 Worker 领取，请勿重复领取",
            actor_field="worker_id",
            actor=worker_id,
            domain_operation=lambda audit_ref, action: task.claim(
                worker_id, expires, audit_ref
            ),
        )
        return task

    async def renew(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> GenerationTask:
        """心跳续租：不迁移状态，只前移到期时间。"""
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await save_versioned_entity(
            task,
            mutation=lambda audit_ref, action: task.renew(
                worker_id, expires, audit_ref
            ),
            audit_ref=f"lease-renew:{uuid4()}",
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            aggregate_id=task.entity_id,
            event_type="GENERATION_LEASE_RENEWED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count,
            },
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="租约续期并发冲突，请重试",
        )
        return task

    async def finish(
        self,
        *,
        task_id: str,
        worker_id: str,
        result: Literal["FAILED"],
        evidence_ref: str = "",
    ) -> GenerationTask:
        """RUNNING → FAILED：只有租约持有者能登记失败。

        成功终态不属于这里：ADR-R42 C1/C3 要求它只能由资产域登记回执驱动的
        T3 推进。这里若仍接受 ``SUCCEEDED``，Worker 就保有自报资产已登记的路径，
        而 QC 与对账拿到的将是一个没有资产事实支撑的成功标记。
        """
        if result != "FAILED":
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="成功终态必须由登记回执（T3）推进，不能由 Worker 自报",
            )
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        target = GenerationTaskStatus.FAILED
        payload = {
            "generation_task_id": task.generation_task_id,
            "worker_id": worker_id,
            "evidence_ref": evidence_ref,
            "attempt_count": task.attempt_count,
        }
        await advance_status(
            task,
            target,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_FAILED",
            tenant_id=task.tenant_id,
            payload=payload,
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="任务终结并发冲突，请重试",
        )
        return task

    async def complete_from_receipt(
        self,
        *,
        generation_task_id: str,
        asset_version_id: str,
        reason: str = "",
    ) -> GenerationTask:
        """T3：以登记回执为唯一凭据推进终态（成功事件由这里发出）。"""
        task = await self._repository.get_by_generation_task_id(generation_task_id)
        if task is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"生成任务不存在: {generation_task_id}",
            )
        task = cast(GenerationTask, task)
        quality_profile_version = task.quality_profile_version
        if task.status in (
            GenerationTaskStatus.SUCCEEDED,
            GenerationTaskStatus.FAILED,
        ):
            # at-least-once 投递的重复消费：终态已由首次消费落库，幂等返回。
            return task
        payload = {
            "generation_task_id": task.generation_task_id,
            "worker_id": "svc:asset-registration",
            "evidence_ref": reason or "asset-registration-receipt",
            "attempt_count": task.attempt_count,
            "project_id": task.project_id,
            "shot_id": task.shot_id,
        }
        if asset_version_id:
            await advance_status(
                task,
                GenerationTaskStatus.SUCCEEDED,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="GenerationTask",
                event_type="GENERATION_SUCCEEDED",
                tenant_id=task.tenant_id,
                payload={
                    **payload,
                    "asset_version_id": asset_version_id,
                    "quality_profile_version": quality_profile_version,
                },
                resource_type="GenerationTask",
                owner_action=ASSET_REGISTRATION_COMPLETED_EVENT,
                conflict_detail="任务已由其他回执推进，请重新读取终态",
                domain_operation=lambda audit_ref, action: task.bind_registration_result(
                    asset_version_id,
                    quality_profile_version,
                    audit_ref,
                    action,
                    GenerationTaskStatus.SUCCEEDED,
                ),
            )
        else:
            await advance_status(
                task,
                GenerationTaskStatus.FAILED,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="GenerationTask",
                event_type="GENERATION_FAILED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="GenerationTask",
                owner_action=ASSET_REGISTRATION_REJECTED_EVENT,
                conflict_detail="任务已由其他回执推进，请重新读取终态",
                domain_operation=lambda audit_ref, action: task.reject_from_registration(
                    audit_ref, action
                ),
            )
        return task

    async def release_expired(self, *, task_id: str) -> GenerationTask:
        """RUNNING → REQUESTED：租约到期回收，任务可被重新领取。

        由扫尾进程调用：判据是 ``lease_expires_at``，与谁持有无关。
        """
        task = await self._load(task_id)
        await advance_status(
            task,
            GenerationTaskStatus.REQUESTED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_LEASE_EXPIRED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": task.lease_owner,
                "attempt_count": task.attempt_count,
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="租约回收并发冲突，请重试",
            domain_operation=lambda audit_ref, action: task.release_expired(
                _now(), audit_ref
            ),
        )
        return task


__all__ = ["GenerationService"]
