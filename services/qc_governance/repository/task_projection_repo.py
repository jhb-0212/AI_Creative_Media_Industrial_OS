"""调度投影的写入端口（同事务维护，无独立 Outbox）。

为什么是「同事务同步」而不是异步双写
--------------------------------
投影的一致性判据只有一条：**同一事务提交后，投影必须等于源任务的当前状态**。
异步双写会在两件事之间留一个窗口：源已经变了、投影还没跟上，而任何以此为输入
的调度都会按旧状态做决定（例如把已经 RUNNING 的任务再发一次领取）。用同一
Session 写入，这个窗口在物理上不存在。

为什么写入条件带 ``entity_version`` 比较
------------------------------------
回填脚本与业务写入可能交错。若允许「低版本覆盖高版本」，一次慢回填就会把投影
退回旧状态，而源表是对的——这种不一致最难查，因为两边都「看起来合理」。
``ON CONFLICT ... WHERE 现有版本 <= 新版本`` 让旧数据无法覆盖新数据，且同一版本
的重放是幂等的。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.metrics import QC_PROJECTION_MISMATCH_TOTAL
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.domain.qc_task import QCTask
from services.qc_governance.repository.projection_orm import TaskProjectionModel

# 只有这两个状态属于「活动投影」。PASSED/REJECTED 是终态：投影行必须被删除，
# 否则调度会一直看到一条永远不会再运行的候选。
_ACTIVE_STATUSES = frozenset({QCTaskStatus.QUEUED.value, QCTaskStatus.RUNNING.value})


def _next_action_at(task: QCTask) -> datetime | None:
    """投影的「何时可被调度」：唯一由源任务字段导出，不引入另一个时间源。

    - QUEUED：入队时间（立即可领）；
    - RUNNING：租约到期时间（到期才可回收）。
    """
    if task.status is QCTaskStatus.QUEUED:
        return task.created_at
    if task.status is QCTaskStatus.RUNNING:
        return task.lease_expires_at
    return None


class SqlAlchemyTaskProjectionRepository:
    """维护 ``qc_maintenance.task_projection``（与源任务同一事务）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sync(self, task: QCTask) -> None:
        """按源任务当前状态同步投影；终态删除行。"""
        if task.status.value not in _ACTIVE_STATUSES:
            await self.retire(task.entity_id)
            return
        values = {
            "task_id": task.entity_id,
            "tenant_id": task.tenant_id,
            "project_id": task.project_id,
            "task_status": task.status.value,
            "next_action_at": _next_action_at(task),
            "lease_expires_at": task.lease_expires_at,
            "lease_generation": task.lease_generation,
            "entity_version": task.entity_version,
            "updated_at": datetime.now(UTC),
        }
        statement = pg_insert(TaskProjectionModel).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[TaskProjectionModel.task_id],
            set_={
                "tenant_id": statement.excluded.tenant_id,
                "project_id": statement.excluded.project_id,
                "task_status": statement.excluded.task_status,
                "next_action_at": statement.excluded.next_action_at,
                "lease_expires_at": statement.excluded.lease_expires_at,
                "lease_generation": statement.excluded.lease_generation,
                "entity_version": statement.excluded.entity_version,
                "updated_at": statement.excluded.updated_at,
            },
            # 低版本不得覆盖高版本；同版本重放幂等。
            where=TaskProjectionModel.entity_version <= statement.excluded.entity_version,
        )
        result = await self._session.execute(statement)
        # ``ON CONFLICT ... WHERE`` 条件不成立时不会有任何行被更新（rowcount=0）：
        # 投影的版本高于源任务。这不该静默发生——它意味着有人在源任务之外改了
        # 状态，或某条写入路径漏了同步。计数让它在 /metrics 上立刻可见，而不是
        # 等到下一次对账才发现「派生数据已经领先事实源」。
        # ``rowcount`` 只存在于 DML 的 CursorResult 上，而 SQLAlchemy 把
        # ``execute`` 的返回类型标注为泛型 ``Result``；用 getattr 取运行时值，
        # 避免为了一句计数把整个语句退回 Core 的另一种执行形式。
        affected = int(getattr(result, "rowcount", 0) or 0)
        if affected == 0:
            QC_PROJECTION_MISMATCH_TOTAL.inc()

    async def retire(self, task_id: str) -> None:
        """删除投影行（任务进入终态，或需要重建）。"""
        await self._session.execute(
            delete(TaskProjectionModel).where(TaskProjectionModel.task_id == task_id)
        )

    async def count_for_tenant(self, tenant_id: object) -> int:
        """对账/测试用：本租户当前投影行数（RLS 仍由会话 GUC 决定可见范围）。"""
        result = await self._session.execute(
            text(
                "SELECT count(*) FROM qc_maintenance.task_projection WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": str(tenant_id)},
        )
        return int(result.scalar_one())


__all__ = ["SqlAlchemyTaskProjectionRepository"]
