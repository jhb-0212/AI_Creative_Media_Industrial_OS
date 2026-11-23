"""QC Worker 进程入口（Phase 3）。

用法：
    python -m workers.qc                       # 常驻（未配置检测器时只扫尾，不领取）
    python -m workers.qc --once                # 只调度一轮
    python -m workers.qc --sweep               # 只回收过期租约
    python -m workers.qc --lease-seconds 180 --idle-sleep 3

默认检测器是 ``AssetIntegrityInspector``：读资产域校验「资产存在 / 项目一致 /
哈希格式 / 内容可验证」，并通过 ``QC.REPORT.CREATE`` 登记报告。它**不做**语义级
画质判断（那需要模型网关与质量档案执行器）。``UnconfiguredInspector`` 仅供
无资产域凭证的部署显式退化为「只扫尾不领取」，避免造假 PASS。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from packages.shared.config import get_process_settings
from packages.shared.database import (
    create_asset_service_engine,
    create_qc_governance_engine,
    create_sessionmaker,
)
from packages.shared.logging import get_logger, setup_logging
from packages.shared.rls import set_tenant_context
from services.asset_service.repository.qc_report_lookup import build_report_lookup
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService
from workers.qc.asset_inspector import AssetIntegrityInspector
from workers.qc.worker import QCExecutor, QCInspection, QCWorker

logger = get_logger(__name__)


class UnconfiguredInspector(QCExecutor):
    """未接真实检测器时的占位：明确「不会检」，而不是假装检过。"""

    configured = False

    async def inspect(self, task: Any) -> QCInspection | None:  # pragma: no cover
        return None


@dataclass
class _QCUnit:
    """一个阶段用的短工作单元：服务与仓储共用同一会话，提交权显式暴露。"""

    service: QCTaskService
    repository: SqlAlchemyQCTaskRepository
    session: Any

    async def commit(self) -> None:
        await self.session.commit()


def _make_scope(sessionmaker: Any, report_lookup: Any | None = None) -> Any:
    """作用域工厂：每阶段一个新会话，服务与仓储共用同一个会话。

    ``report_lookup`` 在这里注入而不是在 ``_build_worker`` 里：判定前核验由
    ``QCTaskService.finish`` 调用，而服务实例是**每个阶段新建**的。若把核验器
    挂在 Worker 上，就得在某处把它转手塞进服务——那一层转手正是「装配了 A、
    实际跑 B」的来源。作用域是服务的唯一构造点，核验器跟着它走。
    """

    @asynccontextmanager
    async def scope() -> AsyncIterator[_QCUnit]:
        async with sessionmaker() as session:
            repository = SqlAlchemyQCTaskRepository(session)
            yield _QCUnit(
                service=QCTaskService(
                    repository,
                    SqlAlchemyOutboxWriter(session),
                    report_lookup=report_lookup,
                ),
                repository=repository,
                session=session,
            )

    return scope


def _default_worker_id() -> str:
    host = socket.gethostname() or "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="R16 QC Worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--idle-sleep", type=float, default=2.0)
    parser.add_argument("--worker-id", default=_default_worker_id())
    parser.add_argument(
        "--no-detector",
        action="store_true",
        help="显式退化为「不领取任务」：没有资产域凭证时只扫尾，不造假 PASS",
    )
    return parser.parse_args(argv)


def _make_report_lookup(asset_sessionmaker: Any) -> Any:
    """判定前核验报告：每次核验独立会话，并显式设置租户上下文。

    必须显式设置：``qc_tasks.tenant_id`` 是队列里给的值，不带进会话就会在 RLS
    下读到 0 行，而「读不到」会被误判成「报告不存在」——真实的越权与真实的缺失
    给出同一个结论，排障时无法区分。

    注入点放在进程入口而不是资产域内：业务代码自行注入 RLS 作用域是 SCOPE-4
    明令禁止的（唯一注入点是 ``build_tenant_uow_dependency``，多一个注入点就多
    一处「以为注入了、其实没注」）。Worker 没有该依赖，因此在这里显式注入，
    读取口径仍复用资产域的 ``build_report_lookup``（一处实现，两个进程共用）。
    """

    async def lookup(qc_report_id: str, tenant_id: Any) -> Any:
        async with asset_sessionmaker() as session:
            await set_tenant_context(session, tenant_id)
            return await build_report_lookup(session)(qc_report_id, tenant_id)

    return lookup


def _build_worker(scope: Any, args: argparse.Namespace, inspector: QCExecutor) -> QCWorker:
    """装配 Worker：作用域工厂 + 检测器 + 租约参数。

    核验器不在这里传：它跟着**作用域**走（见 ``_make_scope``），因为判定前核验
    发生在 ``QCTaskService.finish`` 内部，而服务实例由作用域按阶段创建。放在这里
    传会出现「构造 Worker 时装配了、跑的时候用的是另一个服务」的错位。
    """
    return QCWorker(
        scope=scope,
        executor=inspector,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        idle_sleep=args.idle_sleep,
    )


async def _run_cycle(worker: QCWorker, args: argparse.Namespace) -> bool:
    """一轮调度。各阶段事务边界由 Worker 内部负责。"""
    if args.sweep:
        reclaimed = await worker.sweep_expired()
        logger.info("reclaimed %d expired QC lease(s)", reclaimed)
        return False
    return await worker.run_once()


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_process_settings("qc")
    setup_logging(settings.log_level)
    engine = create_qc_governance_engine()
    sessionmaker = create_sessionmaker(engine)
    asset_engine = None
    report_lookup: Any | None = None
    if args.no_detector:
        inspector: QCExecutor = UnconfiguredInspector()
    else:
        asset_engine = create_asset_service_engine()
        asset_sessionmaker = create_sessionmaker(asset_engine)
        inspector = AssetIntegrityInspector(asset_sessionmaker)
        # 同一个资产会话工厂既用于检测、也用于判定前核验报告：
        # 两处读的是同一份 qc_reports，不可能出现「检测读到了、判定核验不到」。
        report_lookup = _make_report_lookup(asset_sessionmaker)
    # 每阶段一个新工作单元（P1-01）；核验器随作用域注入，判定前核验永远与判定
    # 共用同一个服务实例，不会出现「装配了这个、跑了那个」。
    worker = _build_worker(
        _make_scope(sessionmaker, report_lookup), args, inspector
    )
    try:
        if args.sweep or args.once:
            await _run_cycle(worker, args)
            return 0
        while True:
            try:
                busy = await _run_cycle(worker, args)
            except Exception:
                logger.exception("QC worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(args.idle_sleep)
    finally:
        if asset_engine is not None:
            await asset_engine.dispose()
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
