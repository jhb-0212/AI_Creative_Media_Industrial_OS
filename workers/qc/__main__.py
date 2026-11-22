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
from typing import Any

from packages.shared.config import get_process_settings
from packages.shared.database import (
    create_asset_service_engine,
    create_qc_governance_engine,
    create_sessionmaker,
)
from packages.shared.logging import get_logger, setup_logging
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


def _build_worker(
    session: Any, args: argparse.Namespace, inspector: QCExecutor
) -> QCWorker:
    return QCWorker(
        service=QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        ),
        repository=SqlAlchemyQCTaskRepository(session),
        executor=inspector,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        idle_sleep=args.idle_sleep,
    )


async def _run_cycle(
    sessionmaker: Any, args: argparse.Namespace, inspector: QCExecutor
) -> bool:
    async with sessionmaker() as session:
        worker = _build_worker(session, args, inspector)
        if args.sweep:
            reclaimed = await worker.sweep_expired()
            await session.commit()
            logger.info("reclaimed %d expired QC lease(s)", reclaimed)
            return False
        busy = await worker.process_one()
        if not busy:
            reclaimed = await worker.sweep_expired()
            await session.commit()
            if reclaimed:
                logger.info("idle; reclaimed %d QC lease(s)", reclaimed)
        else:
            await session.commit()
        return busy


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_process_settings("qc")
    setup_logging(settings.log_level)
    engine = create_qc_governance_engine()
    sessionmaker = create_sessionmaker(engine)
    asset_engine = None
    if args.no_detector:
        inspector: QCExecutor = UnconfiguredInspector()
    else:
        asset_engine = create_asset_service_engine()
        inspector = AssetIntegrityInspector(create_sessionmaker(asset_engine))
    try:
        if args.sweep or args.once:
            await _run_cycle(sessionmaker, args, inspector)
            return 0
        while True:
            try:
                busy = await _run_cycle(sessionmaker, args, inspector)
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
