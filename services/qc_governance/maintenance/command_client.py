"""维护命令的唯一调用入口（受约束的拒绝补记协议）。

数据库负责**拒绝**，调用层负责**记录拒绝**——这两件事无法在一条语句里原子完成：
PG 没有自治事务，同一语句里先 INSERT 审计行再 RAISE 会整条回滚。所以本模块把
「回滚失败事务 → 另开干净连接补记」固定成唯一路径，而不是让每个调用点自己
try/except：分散实现时，总有一处会复用 aborted 事务、漏带 attempt_id，或者
干脆把异常吞掉。

这个协议本身有失败窗口（进程崩溃 / 连接中断 / 补记失败），因此：

* **attempt_id 由调用方生成一次、重试复用**：补记幂等，重试不会多记一行。
* **补记失败必须告警**，不能被当成「已经记过了」——丢的是一条真实的拒绝事实。
* **主体与租户由数据库判定**，不接受调用方传入：``record_maintenance_denial``
  取 ``session_user``、按工单查租户。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["MaintenanceCallResult", "MaintenanceCommandClient", "classify_denial"]


@dataclass(frozen=True)
class MaintenanceCallResult:
    """一次维护命令调用的结果。

    ``denial`` 为 None 表示成功；否则是归类后的拒绝原因。``denial_recorded``
    明确区分「已记入账本」与「补记失败」——后者必须被外部告警，不能因为
    返回了拒绝原因就当作账已经平了。
    """

    rows: list[dict[str, object]] = field(default_factory=list)
    denial: str | None = None
    denial_recorded: bool = True
    attempt_id: str | None = None


class MaintenanceCommandClient:
    """统一适配层：执行命令、隔离失败事务、补记拒绝、失败告警。

    ``engine`` 必须是维护登录身份（``qc_maintenance_login``）的引擎，不是业务域
    引擎：命令的权限面由数据库决定，适配层不做任何身份模拟（不 ``SET ROLE``）。
    """

    RECONCILE = "reconcile_tenant_tasks"
    REPAIR = "repair_tenant_task"

    def __init__(
        self, engine: AsyncEngine, *, logger: logging.Logger | None = None
    ) -> None:
        self._engine = engine
        self._logger = logger

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    async def reconcile(
        self, work_order_id: str, *, attempt_id: str
    ) -> MaintenanceCallResult:
        """在工单授权租户内对账；被拒时另开事务补记 DENIED。"""
        return await self._run(
            sql=f"SELECT * FROM qc_maintenance.{self.RECONCILE}(:work_order_id)",
            params={"work_order_id": work_order_id},
            work_order_id=work_order_id,
            attempt_id=attempt_id,
        )

    async def enumerate_due_tasks(self, *, attempt_id: str) -> MaintenanceCallResult:
        return await self._run(
            sql="SELECT * FROM qc_maintenance.enumerate_due_tasks()",
            params={},
            work_order_id=None,
            attempt_id=attempt_id,
        )

    async def detect_anomalies(self, *, attempt_id: str) -> MaintenanceCallResult:
        return await self._run(
            sql="SELECT * FROM qc_maintenance.detect_projection_anomalies()",
            params={},
            work_order_id=None,
            attempt_id=attempt_id,
        )

    async def repair_task(
        self, work_order_id: str, task_id: str, *, attempt_id: str
    ) -> MaintenanceCallResult:
        """在工单授权范围内原子修复单个任务的投影。

        失败区分两类：工单/授权判定属于 ``DENIED``（策略不允许），执行期错误属于
        ``FAILED``。两类都由适配层在回滚后另开事务补记——修复命令自己无法在
        回滚的事务里留下失败行。
        """
        return await self._run(
            sql=(
                f"SELECT * FROM qc_maintenance.{self.REPAIR}"
                "(:work_order_id, :task_id, :attempt_id)"
            ),
            params={
                "work_order_id": work_order_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
            },
            work_order_id=work_order_id,
            attempt_id=attempt_id,
        )

    # ------------------------------------------------------------------ #
    # 协议实现
    # ------------------------------------------------------------------ #
    async def _run(
        self,
        *,
        sql: str,
        params: dict[str, object],
        work_order_id: str | None,
        attempt_id: str,
    ) -> MaintenanceCallResult:
        try:
            # 独立事务：成功时它提交（命令自身在同一事务里落 SUCCEEDED 审计行）。
            async with self._engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                rows = [dict(row._mapping) for row in result]
        except exc.DBAPIError as error:
            # 事务已随上下文管理器回滚。这里**不能**复用那个连接补记：它可能仍
            # 处于 aborted 状态；更关键的是，复用会把「失败事务」和「账本写入」
            # 重新绑成同一个原子单元，正好破坏补记要的独立性。
            reason = classify_denial(error)
            recorded = await self._record_denial(
                work_order_id,
                reason,
                attempt_id,
                action=_action_of(sql),
                outcome=_outcome_of(reason),
            )
            return MaintenanceCallResult(
                rows=[], denial=reason, denial_recorded=recorded, attempt_id=attempt_id
            )
        return MaintenanceCallResult(rows=rows, attempt_id=attempt_id)

    async def _record_denial(
        self,
        work_order_id: str | None,
        reason: str,
        attempt_id: str,
        *,
        action: str,
        outcome: str,
    ) -> bool:
        """补记一行失败侧账；失败返回 False 并告警（不抛，避免掩盖原始拒绝）。"""
        if work_order_id is None:
            # 发现/检测没有工单维度：它们的拒绝只可能来自权限或会话守卫，账本
            # 没有工单可挂。这不是「记过了」，而是「记不了」——必须告警。
            self._warn(
                "维护命令被拒但没有工单可挂账（拒绝事实未入账）",
                reason=reason,
                attempt_id=attempt_id,
            )
            return False
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT qc_maintenance.record_maintenance_denial("
                        ":work_order_id, :reason, :attempt_id, :outcome, :action)"
                    ),
                    {
                        "work_order_id": work_order_id,
                        "reason": reason,
                        "attempt_id": attempt_id,
                        "outcome": outcome,
                        "action": action,
                    },
                )
        except Exception as error:
            self._warn(
                "补记失败：这条失败事实已丢失，需人工核对",
                reason=reason,
                attempt_id=attempt_id,
                error=repr(error),
            )
            return False
        return True

    def _warn(self, message: str, **fields: object) -> None:
        if self._logger is None:
            return
        self._logger.error(message, extra={"maintenance": fields})


# 授权/策略层面的原因记 DENIED；其余（执行期错误、未知）记 FAILED。
_DENIAL_REASONS = frozenset(
    {
        "work_order_not_found",
        "kind_mismatch",
        "not_approved",
        "session_mismatch",
        "task_not_authorized",
        "tenant_mismatch",
        "permission_denied",
    }
)

# 原因 → 稳定短语。顺序敏感：先匹配更具体的拒绝，避免被通用词吃掉。
_DENIAL_TOKENS: tuple[tuple[str, str], ...] = (
    ("未授权修复任务", "task_not_authorized"),
    ("不在工单授权范围内", "tenant_mismatch"),
    ("不存在", "work_order_not_found"),
    ("类型是", "kind_mismatch"),
    ("不得执行", "not_approved"),
    ("不是维护权限组", "session_mismatch"),
)


def classify_denial(error: exc.DBAPIError) -> str:
    """把数据库异常归类成稳定原因（只用于补记口径，不改变拒绝本身）。"""
    message = str(getattr(error, "orig", error))
    for token, reason in _DENIAL_TOKENS:
        if token in message:
            return reason
    if "permission denied" in message or "insufficient_privilege" in message:
        # 入口被 schema/EXECUTE 拒绝时，被拒的那个函数**根本没被调用**，
        # 它无法自己写审计；这一行只能由适配层补记。
        return "permission_denied"
    return "other"


def _outcome_of(reason: str) -> str:
    return "DENIED" if reason in _DENIAL_REASONS else "FAILED"


def _action_of(sql: str) -> str:
    """账本的 action 必须与实际调用的命令一致，不能一律记成 reconcile。"""
    return "repair_tenant_task" if "repair_tenant_task" in sql else "reconcile_tenant_tasks"
