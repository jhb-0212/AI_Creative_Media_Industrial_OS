"""QCTask 聚合根（QC 待检任务，Phase 3）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#QCTask；
- contracts/state_machines/state_machines.yaml → objects → QCTask。

为什么需要这个对象：``QC.REPORT.CREATE`` 只回答「怎么登记一份检测报告」，不回答
「哪一份产物还没被检、检到哪一步、检的人崩了怎么办」。没有待检队列时，QC 只能靠
人记得去点，漏检不会留下任何缺口记录——这是「有动作、没有流程」的典型债。

租约语义与 GenerationTask 同构（领取/心跳/到期回收），但对象、schema、角色完全独立：
QC 任务归 qc_governance，生成任务归 production_planning，不能借一张表跨域。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.domain.state_machine import (
    QC_TASK_LIFECYCLE,
    QC_TASK_PROCESS_ACTION,
)

_OWNER_MODULE = "qc-governance"


def _blank(value: str) -> bool:
    return value is None or not str(value).strip()


@dataclass
class QCTask(Entity):
    """QC 待检任务（QUEUED 出生，PASSED/REJECTED 终态）。"""

    ENTITY_TYPE = "QCTask"

    owner_module: str = _OWNER_MODULE

    qc_task_id: str = ""
    project_id: str = ""
    shot_id: str = ""
    asset_version_id: str = ""
    generation_task_id: str = ""
    quality_profile_version: str = ""
    status: QCTaskStatus = QCTaskStatus.QUEUED
    lease_owner: str = ""
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    qc_report_id: str = ""

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.qc_task_id = self.qc_task_id.strip() or self.entity_id
        for name in (
            "qc_task_id",
            "project_id",
            "asset_version_id",
            "generation_task_id",
            "quality_profile_version",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        for name, max_len in (
            ("qc_task_id", 64),
            ("project_id", 64),
            ("shot_id", 64),
            ("asset_version_id", 64),
            ("generation_task_id", 64),
            ("quality_profile_version", 64),
            ("qc_report_id", 64),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self,
        target: QCTaskStatus,
        audit_ref: str,
        action: str,
    ) -> QCTask:
        """所有状态迁移的唯一入口：边所有权由 Lifecycle 判定。"""
        QC_TASK_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        if target in (QCTaskStatus.PASSED, QCTaskStatus.REJECTED):
            self.lease_owner = ""
            self.lease_expires_at = None
        self._advance_version(audit_ref)
        return self

    # ---- 租约（QC.TASK.PROCESS）------------------------------------------- #

    def require_lease_owner(self, worker_id: str) -> None:
        if self.status is not QCTaskStatus.RUNNING:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"QC 任务当前为 {self.status.value}，不存在有效租约",
            )
        if self.lease_owner != worker_id:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="租约不属于该 Worker：只有持有者才能续租或判定",
            )

    def claim(
        self,
        worker_id: str,
        lease_expires_at: datetime,
        audit_ref: str,
    ) -> QCTask:
        if not worker_id.strip():
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="worker_id 不能为空")
        self.transition_to(QCTaskStatus.RUNNING, audit_ref, QC_TASK_PROCESS_ACTION)
        self.lease_owner = worker_id
        self.lease_expires_at = lease_expires_at
        self.attempt_count += 1
        return self

    def renew(
        self,
        worker_id: str,
        lease_expires_at: datetime,
        audit_ref: str,
    ) -> QCTask:
        self.require_lease_owner(worker_id)
        if lease_expires_at <= (self.lease_expires_at or lease_expires_at):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="新租约到期时间必须晚于当前到期时间"
            )
        self.lease_expires_at = lease_expires_at
        self._advance_version(audit_ref)
        return self

    def finish(
        self,
        worker_id: str,
        target: QCTaskStatus,
        qc_report_id: str,
        audit_ref: str,
    ) -> QCTask:
        """RUNNING → PASSED / REJECTED：判定必须挂报告，空报告等于没有证据。"""
        self.require_lease_owner(worker_id)
        if target not in (QCTaskStatus.PASSED, QCTaskStatus.REJECTED):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"QC 判定只能是 PASSED 或 REJECTED，实际为 {target.value}",
            )
        if _blank(qc_report_id):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="qc_report_id 不能为空"
            )
        self.transition_to(target, audit_ref, QC_TASK_PROCESS_ACTION)
        self.qc_report_id = qc_report_id
        return self

    def release_expired(self, now: datetime, audit_ref: str) -> QCTask:
        if self.status is not QCTaskStatus.RUNNING:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"QC 任务当前为 {self.status.value}，无可回收的租约",
            )
        if self.lease_expires_at is None or self.lease_expires_at > now:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED, detail="租约尚未到期，不可回收"
            )
        self.transition_to(QCTaskStatus.QUEUED, audit_ref, QC_TASK_PROCESS_ACTION)
        self.lease_owner = ""
        self.lease_expires_at = None
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "qc_task_id": self.qc_task_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "asset_version_id": self.asset_version_id,
                "generation_task_id": self.generation_task_id,
                "quality_profile_version": self.quality_profile_version,
                "status": self.status.value,
                "lease_owner": self.lease_owner,
                "lease_expires_at": (
                    self.lease_expires_at.isoformat()
                    if self.lease_expires_at is not None
                    else None
                ),
                "attempt_count": self.attempt_count,
                "qc_report_id": self.qc_report_id,
            }
        )
        return data


__all__ = ["QCTask"]
