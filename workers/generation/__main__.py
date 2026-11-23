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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from adapters.model_gateway.comfyui import ComfyUIGateway
from adapters.model_gateway.openai_compatible import OpenAICompatibleGateway
from adapters.storage.local_fs import LocalFsObjectStorage
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
from workers.generation.asset_registrar import SagaAssetRegistrar
from workers.generation.executor import (
    StorageBackedGenerationExecutor,
    resolve_inline_prompt,
)
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


@dataclass
class _GenerationUnit:
    """一个阶段用的短工作单元：服务与仓储共用同一会话，提交权显式暴露。"""

    service: GenerationService
    repository: SqlAlchemyGenerationTaskRepository
    session: Any

    async def commit(self) -> None:
        await self.session.commit()


def _make_scope(sessionmaker: Any) -> Any:
    """把会话工厂包成「按阶段打开、退出即关闭」的作用域工厂。

    关键点：作用域结束（含异常）时连接立即归还。执行阶段不持有它，因此长耗时
    生成不会把事务与行锁拖到任务结束。
    """

    @asynccontextmanager
    async def scope() -> AsyncIterator[_GenerationUnit]:
        async with sessionmaker() as session:
            repository = SqlAlchemyGenerationTaskRepository(session)
            yield _GenerationUnit(
                service=GenerationService(repository, SqlAlchemyOutboxWriter(session)),
                repository=repository,
                session=session,
            )

    return scope


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
    scope: Any,
    args: argparse.Namespace,
    qc_enqueuer: Any | None = None,
    qc_lookup: Any | None = None,
    executor: Any | None = None,
) -> GenerationWorker:
    return GenerationWorker(
        scope=scope,
        # 默认仍是「诚实失败」占位：未配网关时不伪造产物。
        executor=executor or UnconfiguredExecutor(),
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        idle_sleep=args.idle_sleep,
        qc_enqueuer=qc_enqueuer,
        qc_lookup=qc_lookup,
    )


def _build_executor(settings: Any, sessionmaker: Any) -> tuple[Any, Any | None]:
    """按配置装配执行器，返回 ``(executor, 需要关闭的网关)``。

    三档行为，都是显式的：

    | 配置 | 执行器 | 后果 |
    |---|---|---|
    | 未配 ``MODEL_GATEWAY_BASE_URL`` | ``UnconfiguredExecutor`` | 任务 FAILED，证据写明未配置 |
    | 配了地址但缺 KEY / 存储根 / 工作流模板 | 抛错，进程不启动 | 配置矛盾必须在启动时暴露 |
    | 协议写了无适配器的值 | 抛错，进程不启动 | 否则日志里看不出协议没生效 |
    | 配齐 | ``StorageBackedGenerationExecutor`` | 真实调用（两种协议之一） |

    第二档刻意不降级：若「配了地址但没配 KEY」静默回落到未配置执行器，
    运维会看到「任务一直失败」而去查网关，而真正的原因是本机的配置缺一项。

    资产登记器接 ``SagaAssetRegistrar``：T0/T1 在生成域自己的短事务里落意图并
    发登记请求；资产 ID 由 T2 回执经 relay T3 写回，Worker 不再自报成功。
    """
    base_url = getattr(settings, "model_gateway_base_url", "")
    if not base_url:
        return UnconfiguredExecutor(), None

    if getattr(settings, "storage_backend", "local") != "local":
        raise ValueError(
            "STORAGE_BACKEND 目前只实现 local；s3 端点配置存在但适配器未实现，"
            "不允许静默回落到本地（否则「产物在哪」将无从判断）"
        )
    storage_root = getattr(settings, "storage_local_root", "")
    if not storage_root:
        raise ValueError(
            "已配置 MODEL_GATEWAY_BASE_URL 但 STORAGE_LOCAL_ROOT 为空："
            "产物无处落地，拒绝启动"
        )

    protocol = getattr(settings, "model_gateway_protocol", "openai_compatible")
    gateway = _build_gateway(settings, base_url, protocol)
    storage = LocalFsObjectStorage(
        storage_root,
        max_bytes=getattr(settings, "storage_max_object_bytes", 256 * 1024 * 1024),
    )
    executor = StorageBackedGenerationExecutor(
        gateway=gateway,
        storage=storage,
        resolve_prompt=resolve_inline_prompt,
        registrar=SagaAssetRegistrar(sessionmaker),
        default_model=getattr(settings, "model_gateway_default_model", ""),
        media_type=getattr(settings, "generation_default_media_type", "image/png"),
        size=getattr(settings, "generation_default_size", ""),
    )
    logger.info(
        "generation executor wired to %s (protocol=%s, model=%s, storage_root=%s)",
        base_url,
        protocol,
        getattr(settings, "model_gateway_default_model", "") or "<per-task>",
        storage_root,
    )
    return executor, gateway


def _build_gateway(settings: Any, base_url: str, protocol: str) -> Any:
    """按协议装配网关。

    两条协议**不是**同一个适配器的两种编码，而是两种调用形状：

    - ``openai_compatible``：同步，一次请求拿到字节；
    - ``comfyui``：三段异步（提交工作流 → 轮询历史 → 取产物），且必须有工作流
      模板——ComfyUI 没有任何「给我一句话」的入口。

    因此这里不共用构造参数，各自校验自己缺什么，缺什么就在启动时报什么。
    """
    if protocol == "openai_compatible":
        return OpenAICompatibleGateway(
            base_url=base_url,
            api_key=settings.model_gateway_api_key.get_secret_value(),
            default_model=getattr(settings, "model_gateway_default_model", ""),
            timeout_seconds=getattr(settings, "model_gateway_timeout_seconds", 120.0),
            max_bytes=getattr(
                settings, "model_gateway_max_artifact_bytes", 256 * 1024 * 1024
            ),
            response_format=getattr(
                settings, "model_gateway_response_format", "b64_json"
            ),
        )

    if protocol == "comfyui":
        workflow_path = getattr(settings, "model_gateway_comfyui_workflow_path", "")
        if not workflow_path:
            raise ValueError(
                "MODEL_GATEWAY_PROTOCOL=comfyui 但 "
                "MODEL_GATEWAY_COMFYUI_WORKFLOW_PATH 为空："
                "ComfyUI 没有「给我一句话」的入口，必须提供工作流模板"
                "（在 ComfyUI 里用「导出 (API)」得到）"
            )
        prompt_node = getattr(settings, "model_gateway_comfyui_prompt_node", "")
        if not prompt_node:
            raise ValueError(
                "MODEL_GATEWAY_PROTOCOL=comfyui 但 "
                "MODEL_GATEWAY_COMFYUI_PROMPT_NODE 为空："
                "不指定注入节点就只能靠猜，而猜错会产出与意图无关的图且全程无错"
            )
        return ComfyUIGateway(
            base_url=base_url,
            workflow_path=workflow_path,
            prompt_node=prompt_node,
            prompt_field=getattr(
                settings, "model_gateway_comfyui_prompt_field", "text"
            ),
            negative_node=getattr(settings, "model_gateway_comfyui_negative_node", ""),
            negative_field=getattr(
                settings, "model_gateway_comfyui_negative_field", "text"
            ),
            output_node=getattr(settings, "model_gateway_comfyui_output_node", ""),
            # ComfyUI 下 default_model 表示工作流标识：请求里的 model 必须与它
            # 一致，否则拒绝（见适配器 _check_model 的理由）。
            workflow_id=getattr(settings, "model_gateway_default_model", ""),
            api_key=settings.model_gateway_api_key.get_secret_value(),
            timeout_seconds=getattr(settings, "model_gateway_timeout_seconds", 120.0),
            job_timeout_seconds=getattr(
                settings, "model_gateway_comfyui_job_timeout_seconds", 900.0
            ),
            poll_interval_seconds=getattr(
                settings, "model_gateway_comfyui_poll_interval_seconds", 2.0
            ),
            max_bytes=getattr(
                settings, "model_gateway_max_artifact_bytes", 256 * 1024 * 1024
            ),
        )

    raise ValueError(
        f"MODEL_GATEWAY_PROTOCOL={protocol!r} 无对应适配器"
        "（已实现: openai_compatible / comfyui）"
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


async def _run_cycle(worker: GenerationWorker, args: argparse.Namespace) -> bool:
    """一轮调度。每个阶段的事务边界由 Worker 内部负责，这里不再开外层事务。"""
    if args.sweep:
        reclaimed = await worker.sweep_expired()
        logger.info("reclaimed %d expired lease(s)", reclaimed)
        return False
    return await worker.run_once()


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_process_settings("generation")
    setup_logging(settings.log_level)
    engine = create_production_planning_engine()
    sessionmaker = create_sessionmaker(engine)
    qc_engine = create_qc_governance_engine()
    qc_sessionmaker = create_sessionmaker(qc_engine)
    scope = _make_scope(sessionmaker)
    qc_enqueuer = _make_qc_enqueuer(qc_sessionmaker)
    qc_lookup = _make_qc_lookup(qc_sessionmaker)
    executor, gateway = _build_executor(settings, sessionmaker)
    worker = _build_worker(scope, args, qc_enqueuer, qc_lookup, executor)
    try:
        if args.sweep or args.once:
            await _run_cycle(worker, args)
            return 0
        while True:
            try:
                busy = await _run_cycle(worker, args)
            except Exception:
                logger.exception("generation worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(args.idle_sleep)
    finally:
        if gateway is not None:
            await gateway.aclose()
        await qc_engine.dispose()
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
