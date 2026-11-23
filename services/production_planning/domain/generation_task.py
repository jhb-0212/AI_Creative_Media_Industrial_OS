"""GenerationTask 聚合根（生成任务记录，R16-D3）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#GenerationTask；
- contracts/state_machines/state_machines.yaml → objects → GenerationTask
  （REQUESTED → SUCCEEDED / FAILED 归 GENERATION.REQUEST；
   REQUESTED↔RUNNING、RUNNING→SUCCEEDED/FAILED 四条租约边归 GENERATION.PROCESS）。

两种执行模式共存：
- 同步（R16 MVP）：GENERATION.REQUEST 校验通过后创建 REQUESTED 并同事务推进终态；
- 异步（Phase 2）：任务停在 REQUESTED，由 Worker 持租约领取 → RUNNING，租约到期
  退回 REQUESTED 以便重新领取。

租约为什么必须落在聚合根：租约是「谁有权推进这个任务」的唯一事实。放在 Worker
进程内存里，进程一崩就无从判断任务是被持有还是被遗弃；放在表里才能靠
``lease_expires_at`` 与 ``lease_owner`` 判出「过期可回收」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import GenerationTaskStatus
from services.project_planning.domain.state_machine import (
    GENERATION_PROCESS_ACTION,
    GENERATION_TASK_LIFECYCLE,
)

_OWNER_MODULE = "production-planning"


def _blank(value: str) -> bool:
    return value is None or not str(value).strip()


@dataclass
class GenerationTask(Entity):
    """生成任务记录（REQUESTED 出生，SUCCEEDED/FAILED 终态）。"""

    ENTITY_TYPE = "GenerationTask"

    owner_module: str = _OWNER_MODULE

    generation_task_id: str = ""
    project_id: str = ""
    shot_id: str = ""
    shot_revision_id: str = ""
    production_route_ref: str = ""
    model_adapter_ref: str = ""
    constraint_snapshot_id: str = ""
    budget_ref: str = ""
    prompt_ref: str = ""
    status: GenerationTaskStatus = GenerationTaskStatus.REQUESTED
    # 租约三元组：谁持有、何时到期、领取过几次。第三项用于「同一任务被反复领取
    # 却始终失败」的识别——只看状态看不出是首次执行还是第 9 次崩。
    lease_owner: str = ""
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    # 产物引用：SUCCEEDED 时落库。QC 待检队列和失败补偿扫描都靠它把「任务」
    # 映射到「待检资产」；只放在 Worker 内存里，进程一崩就再也查不回产物。
    asset_version_id: str = ""
    quality_profile_version: str = ""

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.generation_task_id = self.generation_task_id.strip() or self.entity_id
        for name in (
            "generation_task_id",
            "project_id",
            "shot_id",
            "shot_revision_id",
            "production_route_ref",
            "model_adapter_ref",
            "constraint_snapshot_id",
            "budget_ref",
            "prompt_ref",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        for name, max_len in (
            ("generation_task_id", 64),
            ("project_id", 64),
            ("shot_id", 64),
            ("shot_revision_id", 64),
            ("production_route_ref", 64),
            ("model_adapter_ref", 128),
            ("constraint_snapshot_id", 128),
            ("budget_ref", 128),
            ("prompt_ref", 200),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self,
        target: GenerationTaskStatus,
        audit_ref: str,
        action: str,
    ) -> GenerationTask:
        """执行 REQUESTED → SUCCEEDED / FAILED，终态不可原地复活。"""
        GENERATION_TASK_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        if target in (GenerationTaskStatus.SUCCEEDED, GenerationTaskStatus.FAILED):
            # 终态释放租约：留着过期时间会让「已完成」看起来仍被某个 Worker 持有。
            self.lease_owner = ""
            self.lease_expires_at = None
        self._advance_version(audit_ref)
        return self

    # ---- 租约（GENERATION.PROCESS）---------------------------------------- #

    def require_lease_owner(self, worker_id: str, now: datetime | None = None) -> None:
        if self.status is not GenerationTaskStatus.RUNNING:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"任务当前为 {self.status.value}，不存在有效租约",
            )
        if self.lease_owner != worker_id:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="租约不属于该 Worker：只有持有者才能续租或终结",
            )
        # 过期即失效：回收扫描是周期性的，从「到期」到「被扫到」之间存在空窗。
        # 只比对 owner 不比对时间，旧 Worker 就能在这个空窗里续租或终结，
        # 把本该重领的任务变成「由已失去租约的人写入的结果」。
        if self.lease_expires_at is None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="租约为 RUNNING 却缺少到期时间，无法判定是否仍有效",
            )
        moment = now or datetime.now(UTC)
        if moment >= self.lease_expires_at:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"租约已于 {self.lease_expires_at.isoformat()} 过期，"
                    "必须先重新领取才能续租或终结"
                ),
            )

    def claim(
        self,
        worker_id: str,
        lease_expires_at: datetime,
        audit_ref: str,
    ) -> GenerationTask:
        """REQUESTED → RUNNING：领取即落租约，并累加领取次数。"""
        if not worker_id.strip():
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="worker_id 不能为空")
        # 状态只能经 transition_to 改：直接赋值会让「有推进 ⇒ 有事实行」失效，
        # 审计表就永远查不到这次领取（DATA-4）。
        self.transition_to(GenerationTaskStatus.RUNNING, audit_ref, GENERATION_PROCESS_ACTION)
        self.lease_owner = worker_id
        self.lease_expires_at = lease_expires_at
        self.attempt_count += 1
        return self

    def attach_product_refs(
        self,
        asset_version_id: str,
        quality_profile_version: str,
    ) -> GenerationTask:
        """终结前登记产物引用（不改状态、不推版本；由随后的 finish 一并落库）。

        只允许在 RUNNING 且租约期内调用：没有租约的人不能替别的执行者写产物引用，
        否则「谁生成了这份资产」这一事实就无从判定。
        """
        asset_version_id = (asset_version_id or "").strip()
        quality_profile_version = (quality_profile_version or "").strip()
        if not asset_version_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="asset_version_id 不能为空"
            )
        if not quality_profile_version:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="quality_profile_version 不能为空"
            )
        for name, value in (
            ("asset_version_id", asset_version_id),
            ("quality_profile_version", quality_profile_version),
        ):
            if len(value) > 64:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 64",
                )
        self.asset_version_id = asset_version_id
        self.quality_profile_version = quality_profile_version
        return self

    def renew(
        self,
        worker_id: str,
        lease_expires_at: datetime,
        audit_ref: str,
    ) -> GenerationTask:
        """延长租约：不迁移状态，只刷新到期时间（长期任务的心跳）。"""
        self.require_lease_owner(worker_id)
        if lease_expires_at <= (self.lease_expires_at or lease_expires_at):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="新租约到期时间必须晚于当前到期时间"
            )
        self.lease_expires_at = lease_expires_at
        self._advance_version(audit_ref)
        return self

    def release_expired(self, now: datetime, audit_ref: str) -> GenerationTask:
        """RUNNING → REQUESTED：租约过期退回，任务可被重新领取。

        判据只看 ``lease_expires_at`` 与当前时间：谁持有不重要——持有者已经沉默，
        沉默就是遗弃。
        """
        if self.status is not GenerationTaskStatus.RUNNING:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"任务当前为 {self.status.value}，无可回收的租约",
            )
        if self.lease_expires_at is None or self.lease_expires_at > now:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED, detail="租约尚未到期，不可回收"
            )
        self.transition_to(
            GenerationTaskStatus.REQUESTED, audit_ref, GENERATION_PROCESS_ACTION
        )
        self.lease_owner = ""
        self.lease_expires_at = None
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "generation_task_id": self.generation_task_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "shot_revision_id": self.shot_revision_id,
                "production_route_ref": self.production_route_ref,
                "model_adapter_ref": self.model_adapter_ref,
                "constraint_snapshot_id": self.constraint_snapshot_id,
                "budget_ref": self.budget_ref,
                "prompt_ref": self.prompt_ref,
                "status": self.status.value,
                "lease_owner": self.lease_owner,
                "lease_expires_at": (
                    self.lease_expires_at.isoformat()
                    if self.lease_expires_at is not None
                    else None
                ),
                "attempt_count": self.attempt_count,
                "asset_version_id": self.asset_version_id,
                "quality_profile_version": self.quality_profile_version,
            }
        )
        return data


__all__ = ["GenerationTask"]
