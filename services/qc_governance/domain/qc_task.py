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
from datetime import UTC, datetime
from typing import Any, Literal

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


# 入队来源：只有两种，且**必须是闭集**。
#
# 为什么不做成自由字符串：来源是审计判据。写成 "manual" / "MANUAL" / "人工"
# 都能过校验时，「这条待检项是人建的还是自动来的」就再也统计不出来——
# 而那正是 C7-6 要求可追溯的第一项。闭集让取值错误在类型检查期暴露。
QCTaskEnqueueSource = Literal["RELAY", "MANUAL"]


@dataclass(frozen=True)
class EnqueueOrigin:
    """一次入队动作的来源事实（写进 ``QC_TASK_QUEUED`` 载荷，构成审计凭据）。

    为什么来源要随事件落库，而不是只记在日志里
    ----------------------------------------
    自动入队与人工恢复**共用同一条幂等事实**（同一个 ``generation_task_id``
    唯一键），所以两者在表里长得一模一样。事后要回答「这条待检项是 relay
    投递出来的，还是某个运维人员手工补的」，只能靠事件载荷——日志会轮转，
    outbox 行不会。

    ``reason`` 对自动路径为空：自动入队不需要解释自己为什么存在；
    人工路径必须给出理由（由服务层强制），因为「人为什么绕过正常链路」
    是这次操作唯一无法从数据推导出来的信息。
    """

    source: QCTaskEnqueueSource
    operator_id: str
    reason: str = ""

    def __post_init__(self) -> None:
        if _blank(self.operator_id):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="入队来源缺少 operator_id"
            )
        if self.source == "MANUAL" and _blank(self.reason):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="人工恢复入队必须给出 reason（操作理由不可留空）",
            )


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
    # 租约代次：**单调递增**的 fencing token，与 attempt_count / entity_version 语义不同。
    #   attempt_count  = 这条任务被领取过几次（重试计数，可读性用途）
    #   entity_version = 实体版本（乐观锁）
    #   lease_generation = 当前有效租约的代号；每次「新租约成立」递增，心跳不递增。
    # 为什么必须有独立代次：仅比对 owner + 过期时间挡不住「同一 worker_id 重新领取后，
    # 旧进程用旧租约回写」——owner 相同、时间也未过期（新租约刚建），旧写入会被放行。
    # 代次让「旧租约」在数值上就与新租约不同，旧进程无法凭自己手里的旧值通过校验。
    lease_generation: int = 0
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
        if self.lease_generation < 0:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="lease_generation 不能为负"
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

    def require_lease_owner(
        self,
        worker_id: str,
        expected_lease_generation: int,
        now: datetime | None = None,
    ) -> None:
        """校验「调用方就是当前租约持有人」，且持有的是**这一代**租约。

        为什么 ``expected_lease_generation`` 必须显式传、不能有默认值
        ---------------------------------------------------------
        默认值会让旧调用方静默退化成「只比对 owner」的旧 fencing：同一个 worker_id
        被重新领取后（worker 进程重启、容器重调度），旧进程手里的过时结论仍然能通过
        owner 校验——它的租约「看起来」还有效，因为新租约刚建、时间没到。代次是唯一
        能把这两次持有区分开的字段，缺了就等于没有 fencing。因此这里刻意不提供
        ``None`` 的兼容分支。
        """
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
        # 同生成域：过期到被扫到之间有窗口，只比对 owner 会让旧 Worker 在该窗口内
        # 继续续租或判定，产出「由已失去租约的人写的结论」。
        if self.lease_expires_at is None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="租约为 RUNNING 却缺少到期时间，无法判定是否仍有效",
            )
        if expected_lease_generation != self.lease_generation:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    "租约代次已过期：调用方持有 generation="
                    f"{expected_lease_generation}，当前为 {self.lease_generation}。"
                    "旧租约的写入必须被拒绝，防止已失去租约的 Worker 回写结论"
                ),
            )
        moment = now or datetime.now(UTC)
        if moment >= self.lease_expires_at:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"租约已于 {self.lease_expires_at.isoformat()} 过期，"
                    "必须先重新领取才能续租或判定"
                ),
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
        # 成立一份**新**租约：代次前移。attempt_count 只说明「领过几次」，不承担
        # fencing 职责——两者不可互相替代（代次还包含回收与换手）。
        self.lease_generation += 1
        self.attempt_count += 1
        return self

    def renew(
        self,
        worker_id: str,
        expected_lease_generation: int,
        lease_expires_at: datetime,
        audit_ref: str,
    ) -> QCTask:
        # 心跳只延长到期时间，**不**推进代次：否则每次心跳都会让 Worker 手里的
        # 值失效，续租与判定就无法在同一份租约内完成。
        self.require_lease_owner(worker_id, expected_lease_generation)
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
        expected_lease_generation: int,
        target: QCTaskStatus,
        qc_report_id: str,
        audit_ref: str,
    ) -> QCTask:
        """RUNNING → PASSED / REJECTED：判定必须挂报告，空报告等于没有证据。"""
        self.require_lease_owner(worker_id, expected_lease_generation)
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
        # 回收也是一次「租约终结」：代次前移，旧 Worker 手里的 generation 立即失效。
        self.lease_generation += 1
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
                "lease_generation": self.lease_generation,
                "qc_report_id": self.qc_report_id,
            }
        )
        return data


__all__ = ["EnqueueOrigin", "QCTask", "QCTaskEnqueueSource"]
