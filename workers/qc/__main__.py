"""QC Worker 进程入口（Phase 3）。

用法：
    python -m workers.qc                       # 常驻（未配置检测器时只扫尾，不领取）
    python -m workers.qc --once                # 只调度一轮
    python -m workers.qc --sweep               # 只回收过期租约
    python -m workers.qc --lease-seconds 180 --idle-sleep 3

默认检测器是 ``UnconfiguredInspector``（configured=False）：没有真实检测能力时
Worker 不领取任务，队列会停在 QUEUED 并持续告警。这是刻意的——假 PASS 会直接
污染 Gate 的事实基础。
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
from packages.shared.database import create_qc_governance_engine, create_sessionmaker
from packages.shared.logging import get_logger, setup_logging
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService
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
    return parser.parse_args(argv)


def _build_worker(session: Any, args: argparse.Namespace) -> QCWorker:
    return QCWorker(
        service=QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        ),
        repository=SqlAlchemyQCTaskRepository(session),
        executor=UnconfiguredInspector(),
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        idle_sleep=args.idle_sleep,
    )


async def _run_cycle(sessionmaker: Any, args: argparse.Namespace) -> bool:
    async with sessionmaker() as session:
        worker = _build_worker(session, args)
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
    try:
        if args.sweep or args.once:
            await _run_cycle(sessionmaker, args)
            return 0
        while True:
            try:
                busy = await _run_cycle(sessionmaker, args)
            except Exception:
                logger.exception("QC worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(args.idle_sleep)
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
