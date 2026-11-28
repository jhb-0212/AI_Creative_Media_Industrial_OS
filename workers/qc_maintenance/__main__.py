"""维护命令进程（WP-B）：``python -m workers.qc_maintenance``。

为什么它必须是**独立进程**
--------------------------
维护命令（对账/修复/检测）以 ``qc_maintenance_login`` 身份执行，命令本身是
SECURITY DEFINER——真正的读写发生在维护 owner 名下。这个进程：

* 不建 ``qc_governance`` 池：读不到任务表，也无法顺带改队列；
* 不建 ``asset_service`` 池：不登记报告；
* 不建调度发现池：发现是另一个身份的事。

若把维护命令挂在 QC Worker 进程里，QC 池与维护登录池就会同时存在于一个
进程，那条「维护只能经命令触达数据」的边界只能靠代码自觉——而边界靠自觉时，
它就不再是边界。

用法::

    python -m workers.qc_maintenance reconcile --work-order WO-...
    python -m workers.qc_maintenance repair --work-order WO-... --task <task-id>
    python -m workers.qc_maintenance detect

``--attempt`` 可显式指定：同一逻辑调用的重试必须复用同一个 attempt（补记幂等）。
不给则由本进程生成一次并打印，便于失败后原样重试。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from packages.shared.config import get_process_settings
from packages.shared.database import create_qc_maintenance_engine
from packages.shared.logging import setup_logging
from services.qc_governance.maintenance.command_client import (
    MaintenanceCallResult,
    MaintenanceCommandClient,
)

# 这里用 stdlib logger 而不是仓库里的结构化 logger：适配层签名就是
# ``logging.Logger``（它走 ``extra`` 字段），换成结构化 logger 只能靠类型忽略，
# 而类型忽略会掩盖「字段没落对地方」这类问题。
logger = logging.getLogger(__name__)

# 退出码：脚本要能区分「成功 / 被拒 / 拒了但没记上账 / 前置不满足」。
EXIT_OK = 0
EXIT_ISOLATION_REFUSED = 2
EXIT_DENIED = 4
EXIT_DENIAL_NOT_RECORDED = 5

_IDENTITY_SQL = text(
    """
    SELECT
        current_user AS role,
        (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,
        (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass_rls,
        has_schema_privilege(current_user, 'qc_governance', 'USAGE') AS qc_schema_usage
    """
)

# 用 OID 而不是「schema.table」文本查权限：后者要求会话对那个 schema 有 USAGE
# 才能解析名字，于是「没有权限」会以 ``permission denied for schema`` 抛出来，
# 把「查权限」变成一次依赖被查权限的调用——校验器自己先撞墙，断言就永远跑不到。
_TABLE_PRIVILEGE_SQL = text(
    """
    SELECT has_table_privilege(current_user, c.oid, 'SELECT')
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND c.relname = :table
    """
)

_FUNCTION_PRIVILEGE_SQL = text(
    """
    SELECT has_function_privilege(current_user, p.oid, 'EXECUTE')
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = :schema AND p.proname = :name
    """
)


async def verify_isolation(engine: AsyncEngine) -> None:
    """启动期校验：维护身份只能经命令触达数据。

    为什么在启动期：授权面是迁移期事实，进程运行期看不到它。若有人给
    ``qc_maintenance_login`` 补了一句表读权限，进程会照常启动、照常「只跑命令」，
    直到某天有人顺着这条路径直接读队列。把声明变成启动断言，扩权即启动失败。
    """
    async with engine.connect() as conn:
        identity = (await conn.execute(_IDENTITY_SQL)).one()

        async def table_read(schema: str, table: str) -> bool:
            return bool(await conn.scalar(_TABLE_PRIVILEGE_SQL, {"schema": schema, "table": table}))

        async def function_exec(name: str) -> bool:
            return bool(
                await conn.scalar(
                    _FUNCTION_PRIVILEGE_SQL,
                    {"schema": "qc_maintenance", "name": name},
                )
            )

        can_read_tasks = await table_read("qc_governance", "qc_tasks")
        can_read_projection = await table_read("qc_maintenance", "task_projection")
        can_reconcile = await function_exec("reconcile_tenant_tasks")
        can_repair = await function_exec("repair_tenant_task")
        can_detect = await function_exec("detect_projection_anomalies")

    problems: list[str] = []
    if identity.is_superuser:
        problems.append("维护身份是超级用户：命令的 owner 边界对超级用户不成立")
    if identity.bypass_rls:
        problems.append("维护身份带 BYPASSRLS：全域读能力与「只经命令」互斥")
    if identity.qc_schema_usage:
        problems.append("维护身份对 qc_governance 有 USAGE：可以直接查队列 schema")
    if can_read_tasks:
        problems.append("维护身份可直读 qc_tasks：命令不再是唯一触达路径")
    if can_read_projection:
        problems.append("维护身份可直读 task_projection：发现/对账口径被绕过")
    if not can_repair:
        problems.append("维护身份不能执行修复命令：进程存在也无意义")
    if not can_reconcile:
        problems.append("维护身份不能执行对账命令")
    if not can_detect:
        problems.append("维护身份不能执行检测命令")
    if problems:
        raise RuntimeError(f"维护身份 {identity.role} 与声明不符：" + "；".join(problems))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m workers.qc_maintenance", description="R16 QC 维护命令"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reconcile = sub.add_parser("reconcile", help="在工单授权租户内对账")
    reconcile.add_argument("--work-order", required=True)
    reconcile.add_argument("--attempt", default=None)

    repair = sub.add_parser("repair", help="在工单授权范围内修复单个任务投影")
    repair.add_argument("--work-order", required=True)
    repair.add_argument("--task", required=True)
    repair.add_argument("--attempt", default=None)

    detect = sub.add_parser("detect", help="全域差异计数（只报告不修改）")
    detect.add_argument("--attempt", default=None)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, client: MaintenanceCommandClient) -> int:
    attempt_id = args.attempt or uuid.uuid4().hex
    if args.command == "reconcile":
        result = await client.reconcile(args.work_order, attempt_id=attempt_id)
    elif args.command == "repair":
        result = await client.repair_task(args.work_order, args.task, attempt_id=attempt_id)
    else:
        result = await client.detect_anomalies(attempt_id=attempt_id)

    print(
        json.dumps(
            {
                "command": args.command,
                "attempt_id": result.attempt_id,
                "denial": result.denial,
                # 成功时 denial_recorded 的默认值是 True（没有拒绝事实要记）。
                # 直接透传会让成功输出看起来像「记了一次拒绝」，所以在没有
                # 拒绝时明确给 False：输出里的每个字段都要能被当作事实读。
                "denial_recorded": (result.denial is not None and result.denial_recorded),
                "rows": [{k: str(v) for k, v in row.items()} for row in result.rows],
            },
            ensure_ascii=False,
        )
    )
    return _exit_code(result)


def _exit_code(result: MaintenanceCallResult) -> int:
    if result.denial is None:
        return EXIT_OK
    if not result.denial_recorded:
        # 被拒了、但失败事实没进账本：这不是成功，也不是普通的「被拒」。
        return EXIT_DENIAL_NOT_RECORDED
    return EXIT_DENIED


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_process_settings("qc_maintenance")
    setup_logging(settings.log_level)
    engine = create_qc_maintenance_engine(settings)
    try:
        try:
            await verify_isolation(engine)
        except RuntimeError as exc:
            logger.error("maintenance process refused: %s", exc)
            return EXIT_ISOLATION_REFUSED
        return await _run(args, MaintenanceCommandClient(engine, logger=logger))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
