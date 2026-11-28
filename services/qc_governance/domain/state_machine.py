"""qc_governance 域状态机（QCTask，Phase 3）。

契约权威：contracts/state_machines/state_machines.yaml → objects → QCTask
（QUEUED → RUNNING → PASSED / REJECTED，RUNNING → QUEUED 为租约到期回收）。
四条边全部归 ``QC.TASK.PROCESS``：入队（QUEUED 出生）由 ``QC.TASK.ENQUEUE`` 负责，
不产生状态迁移。

为什么放在本域而不是 project_planning 的 state_machine.py：QC 任务是 qc_governance
自有对象（R16-D3-04），与生成任务分属两个 schema、两个角色。状态机跟着对象走，
避免「对象在 A 域、边声明在 B 域」的跨域耦合。
"""

from __future__ import annotations

from packages.shared.lifecycle import Lifecycle, Transition
from services.project_planning.domain.enums import QCTaskStatus

QC_TASK_PROCESS_ACTION = "QC.TASK.PROCESS"
QC_TASK_ENQUEUE_ACTION = "QC.TASK.ENQUEUE"

QC_TASK_TRANSITIONS: tuple[Transition[QCTaskStatus], ...] = (
    Transition(QCTaskStatus.QUEUED, QCTaskStatus.RUNNING, QC_TASK_PROCESS_ACTION),
    Transition(QCTaskStatus.RUNNING, QCTaskStatus.PASSED, QC_TASK_PROCESS_ACTION),
    Transition(QCTaskStatus.RUNNING, QCTaskStatus.REJECTED, QC_TASK_PROCESS_ACTION),
    Transition(QCTaskStatus.RUNNING, QCTaskStatus.QUEUED, QC_TASK_PROCESS_ACTION),
)

QC_TASK_LIFECYCLE = Lifecycle(
    object_name="QCTask",
    states=tuple(QCTaskStatus),
    edges=QC_TASK_TRANSITIONS,
    initial_states=(QCTaskStatus.QUEUED,),
    frozen_states=(),
)

QC_TASK_ALLOWED_TRANSITIONS: dict[QCTaskStatus, frozenset[QCTaskStatus]] = (
    QC_TASK_LIFECYCLE.allowed_transitions()
)


__all__ = [
    "QC_TASK_ALLOWED_TRANSITIONS",
    "QC_TASK_ENQUEUE_ACTION",
    "QC_TASK_LIFECYCLE",
    "QC_TASK_PROCESS_ACTION",
    "QC_TASK_TRANSITIONS",
]
