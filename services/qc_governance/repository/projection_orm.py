"""qc_maintenance.task_projection 的 ORM（调度投影，可重建，不是事实源）。

为什么投影与被投影的表分 schema
------------------------------
``qc_tasks`` 归 ``qc_governance`` 域、由 ``qc_governance_role`` 持有；投影是
**调度视图**，将来只由调度身份读取。把它放进独立 schema ``qc_maintenance``，
才能在数据库层做到「调度身份只能读投影，读不到业务表」——同一 schema 里做不到
这件事，因为 GRANT 是 schema 内按表授的，但 schema 的 USAGE 会同时暴露两者。
若两者同 schema，任何「只给调度读投影」的尝试都会顺带给出业务表的可见性。

投影必须可重建：它是 ``qc_tasks`` 的派生数据，任何差异都以源表为准（见
``docs/audits/2026-11-26-controlled-maintenance-unified-design.md``）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import project_tenant_fk

SCHEMA_NAME = "qc_maintenance"
TABLE_NAME = "task_projection"


class TaskProjectionModel(Base):
    """一行对应一个活动的 QC 任务（QUEUED / RUNNING）。"""

    __tablename__ = TABLE_NAME
    __table_args__ = (
        # 调度索取：QUEUED 按 next_action_at 取候选。
        Index(
            "ix_qc_projection_queued",
            "next_action_at",
            "task_id",
            postgresql_where=text("task_status = 'QUEUED'"),
        ),
        # 过期回收：RUNNING 按 lease_expires_at 取候选。
        Index(
            "ix_qc_projection_running",
            "lease_expires_at",
            "task_id",
            postgresql_where=text("task_status = 'RUNNING'"),
        ),
        # 归属真实性：投影行必须指向**真实存在且同租户**的项目。
        # 派生数据也需要这条约束——否则一行写错租户的投影会出现在不属于它的
        # 租户候选里，而调度据此做的决定没有任何上层能纠正。
        project_tenant_fk(TABLE_NAME),
        {"schema": SCHEMA_NAME},
    )

    # 主键就是源任务的 entity_id：投影不引入自己的 id 空间，避免「同一任务两个 id」。
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 可调度时刻：QUEUED 取入队时间（立即可领），RUNNING 取租约到期时间。
    # 不引入第二套时间源——它必须能由 ``lease_expires_at`` / ``created_at`` 导出。
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["SCHEMA_NAME", "TABLE_NAME", "TaskProjectionModel"]
