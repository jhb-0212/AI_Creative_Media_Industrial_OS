"""生成 Worker 进程入口（R16 Phase 2 §3）。

用法：
    python -m workers.generation                     # 常驻，空闲时按 --idle-sleep 轮询
    python -m workers.generation --once              # 只调度一轮后退出
    python -m workers.generation --sweep             # 只回收过期租约后退出
    python -m workers.generation --lease-seconds 120 --idle-sleep 2

为什么单独一个进程：领取任务需要「跨请求存活」的执行上下文，放进 API 请求生命周期
会在请求结束时丢租约；Worker 崩溃后由租约过期回收兜底，不需要人工介入。

执行器默认是「诚实失败」占位：未接真实生成网关时任务以 FAILED 落终态并写明
evidence_ref，不伪造产物。真实网关通过 GenerationWorker(executor=...) 注入。
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
    create_production_planning_engine,
    create_qc_governance_engine,
    create_sessionmaker,
)
from packages.shared.logging import get_logger, setup_logging
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService
from workers.generation.worker import (
    GenerationExecutor,
    GenerationOutcome,
    GenerationWorker,
)

logger = get_logger(__name__)


class UnconfiguredExecutor(GenerationExecutor):
    """未接真实生成网关时的占位执行器：如实失败，不伪造产物。"""

    async def execute(self, task: Any) -> GenerationOutcome:
        logger.warning(
            "no generation gateway configured; task %s marked FAILED", task.entity_id
        )
        return GenerationOutcome(
            result="FAILED",
            evidence_ref="generation-gateway-not-configured",
        )


def _default_worker_id() -> str:
    host = socket.gethostname() or "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="R16 生成 Worker")
    parser.add_argument("--once", action="store_true", help="只调度一轮后退出")
    parser.add_argument("--sweep", action="store_true", help="只回收过期租约后退出")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--idle-sleep", type=float, default=1.0)
    parser.add_argument("--worker-id", default=_default_worker_id())
    return parser.parse_args(argv)


def _build_worker(
    session: Any,
    args: argparse.Namespace,
    qc_enqueuer: Any | None = None,
    qc_lookup: Any | None = None,
) -> GenerationWorker:
    return GenerationWorker(
        service=GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        ),
        repository=SqlAlchemyGenerationTaskRepository(session),
        executor=UnconfiguredExecutor(),
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        idle_sleep=args.idle_sleep,
        qc_enqueuer=qc_enqueuer,
        qc_lookup=qc_lookup,
    )


def _make_qc_enqueuer(qc_sessionmaker: Any) -> Any:
    """成功产物入 QC 队列：独立事务，入队失败不回滚生成终态。"""

    async def enqueue(task: Any, outcome: Any) -> None:
        async with qc_sessionmaker() as session:
            service = QCTaskService(
                SqlAlchemyQCTaskRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            await service.enqueue(
                project_id=task.project_id,
                shot_id=task.shot_id,
                asset_version_id=outcome.asset_version_id,
                generation_task_id=task.entity_id,
                quality_profile_version=outcome.quality_profile_version,
                created_by="generation-worker",
                tenant_id=task.tenant_id,
            )
            await session.commit()

    return enqueue


def _make_qc_lookup(qc_sessionmaker: Any) -> Any:
    """对账用：该生成任务是否已有待检项（跨域只读查询，独立会话）。"""

    async def exists(generation_task_id: str) -> bool:
        async with qc_sessionmaker() as session:
            repo = SqlAlchemyQCTaskRepository(session)
            return await repo.find_by_generation_task(generation_task_id) is not None

    return exists


async def _run_cycle(
    sessionmaker: Any,
    args: argparse.Namespace,
    qc_enqueuer: Any | None = None,
    qc_lookup: Any | None = None,
) -> bool:
    """一个事务跑一轮：领任务或（空闲时）补漏入队 + 扫尾，提交后归还连接。"""
    async with sessionmaker() as session:
        worker = _build_worker(session, args, qc_enqueuer, qc_lookup)
        if args.sweep:
            reclaimed = await worker.sweep_expired()
            await session.commit()
            logger.info("reclaimed %d expired lease(s)", reclaimed)
            return False
        busy = await worker.process_one()
        if not busy:
            await worker.reconcile_qc()
            reclaimed = await worker.sweep_expired()
            await session.commit()
            if reclaimed:
                logger.info("idle; reclaimed %d lease(s)", reclaimed)
        else:
            await session.commit()
        return busy


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_process_settings("generation")
    setup_logging(settings.log_level)
    engine = create_production_planning_engine()
    sessionmaker = create_sessionmaker(engine)
    qc_engine = create_qc_governance_engine()
    qc_sessionmaker = create_sessionmaker(qc_engine)
    qc_enqueuer = _make_qc_enqueuer(qc_sessionmaker)
    qc_lookup = _make_qc_lookup(qc_sessionmaker)
    try:
        if args.sweep or args.once:
            await _run_cycle(sessionmaker, args, qc_enqueuer, qc_lookup)
            return 0
        while True:
            try:
                busy = await _run_cycle(sessionmaker, args, qc_enqueuer, qc_lookup)
            except Exception:
                logger.exception("generation worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(args.idle_sleep)
    finally:
        await qc_engine.dispose()
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
