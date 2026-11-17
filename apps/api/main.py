"""FastAPI 应用入口（apps/api）。

职责（薄层原则）：
- 装配 FastAPI 应用、注册异常处理器与中间件。
- 提供健康检查与错误码探测端点。
- 业务逻辑下沉到 services，本层只做协议适配。

契约来源：contracts/openapi/openapi.draft.yaml（后续逐步对齐路由）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from apps.api.resource_ownership import CompositeResourceOwnershipPort
from packages.errors.error_codes import (
    DomainError,
    ErrorCode,
    ErrorRegistry,
    to_response_payload,
)
from packages.identity.guards import assert_no_signing_key_in_api_process
from packages.shared.config import get_settings
from packages.shared.database import lifespan as database_lifespan
from packages.shared.db_roles import DatabaseDomain
from packages.shared.logging import get_logger, set_trace_id, setup_logging
from packages.shared.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    RATE_LIMIT_DECISIONS_TOTAL,
    metrics_response,
)
from packages.shared.rate_limit import RateLimitDecision
from packages.telemetry.tracing import setup_telemetry
from services.identity_access import get_public_key_store
from services.identity_access.deps import reload_verification_keys_if_file_changed

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("apps.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程启动/关闭（身份 + 数据库两类进程级资源）。

    **启动即校验身份配置**（包 B4）：

    - 配置里有签发私钥 → 拒绝启动（``assert_no_signing_key_in_api_process``）；
    - 没有验签公钥 → 拒绝启动（``get_public_key_store`` fail-closed）。

    为什么放在 lifespan 而不是模块导入时：导入期校验会误伤"只需要路由表"的
    用法（OpenAPI 导出、契约检查、路由表单测），让它们因为一个无关的密钥配置
    而失败。启动点是"这个进程要开始接流量"的唯一时刻，配置缺失必须在这里挡下。

    **JWKS 热轮换**（ADR-R29）：验签公钥来自 JWKS 文件且
    ``IDENTITY_JWKS_REFRESH_SECONDS`` > 0 时，启动轮询任务周期性重读文件，
    文档变化即整体替换验签公钥——签发端追加新 kid 后验签端无重启拾取。
    """
    assert_no_signing_key_in_api_process(settings)
    get_public_key_store()
    jwks_refresh_task = _start_jwks_refresh_if_configured()
    try:
        async with database_lifespan(app):
            # 跨域资源归属解析端口（组合根装配）：审批发起时校验目标资源
            # 存在且属于票据项目（R4-2 收口）。池就绪后才可构造。
            app.state.resource_ownership_port = CompositeResourceOwnershipPort(
                app.state.pools
            )
            yield
    finally:
        if jwks_refresh_task is not None:
            jwks_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await jwks_refresh_task


def _start_jwks_refresh_if_configured() -> asyncio.Task[None] | None:
    """按配置启动 JWKS 热轮换轮询任务（不满足前提返回 None，不启动）。

    前提与 ``reload_verification_keys_if_file_changed`` 一致：验签公钥必须
    来自 JWKS 文件（PEM 来源轮换伴随进程重启，热刷新无意义）。
    """
    if settings.identity_jwks_refresh_seconds <= 0:
        return None
    if settings.identity_public_key_pem.get_secret_value().strip():
        return None
    if not settings.identity_jwks_file.strip():
        return None
    return asyncio.create_task(
        _jwks_refresh_loop(settings.identity_jwks_refresh_seconds)
    )


async def _jwks_refresh_loop(interval_seconds: int) -> None:
    """周期性重读 JWKS 文件，轮换发生时留下可追溯日志。

    单次失败只记日志、不影响下一轮：公钥仓储未变更前验签继续用旧密钥，
    行为等同"该轮轮换未发生"（fail-closed 且无损）。
    """
    logger.info(
        "jwks_refresh_loop_started",
        interval_seconds=interval_seconds,
        jwks_file=settings.identity_jwks_file,
    )
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            if reload_verification_keys_if_file_changed():
                logger.info("jwks_rotated", jwks_file=settings.identity_jwks_file)
        except Exception:
            logger.error("jwks_refresh_failed", exc_info=True)


app = FastAPI(
    title="AI Creative Media Industrial OS",
    version=settings.app_version,
    description="AI 影视工业化生产平台 API（模块化单体 + 异步 Worker）",
    lifespan=lifespan,
)

# 接入 OpenTelemetry 分布式追踪（未配置 OTLP endpoint 时优雅降级）。
setup_telemetry(app)

# 挂载领域模块路由。
from services.asset_service.api.asset_routes import router as asset_router  # noqa: E402
from services.asset_service.api.delivery_routes import (  # noqa: E402
    router as delivery_router,
)
from services.project_planning.api.episode_routes import router as episode_router  # noqa: E402
from services.project_planning.api.project_routes import router as project_router  # noqa: E402
from services.project_planning.api.script_routes import router as script_router  # noqa: E402
from services.qc_governance.api.gate_routes import router as gate_router  # noqa: E402
from services.shot_planning.api.constraint_pack_routes import (  # noqa: E402
    router as constraint_pack_router,
)
from services.shot_planning.api.shot_routes import router as shot_router  # noqa: E402
from services.shot_planning.api.spec_routes import router as spec_router  # noqa: E402
from services.workflow.api.approval_routes import router as workflow_router  # noqa: E402

app.include_router(project_router)
app.include_router(script_router)
app.include_router(episode_router)
app.include_router(shot_router)
app.include_router(spec_router)
app.include_router(constraint_pack_router)
app.include_router(asset_router)
app.include_router(delivery_router)
app.include_router(gate_router)
app.include_router(workflow_router)


# 探针、指标与 JWKS 发布端点不参与限流：探针被编排系统高频抓取，
# JWKS 被外部验签方在令牌风暴期间高频拉取——被限流会把"密钥分发正常"
# 误判成"实例不可用/密钥源失效"，放大而非缓解故障。
_RATE_LIMIT_EXEMPT_PATHS = frozenset(
    {"/health", "/readyz", "/metrics", "/.well-known/jwks.json"}
)


@app.middleware("http")
async def request_guard_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """限流 + 指标采集（注册早于访问日志，因此位于其内层）。

    放在内层是有意的：被限流拒绝的请求同样要经过外层访问日志，
    否则 429 在日志里是「不存在」的，排障时看不到被挡了多少。
    """
    method = request.method
    start = time.perf_counter()
    status_code = 500

    try:
        if request.url.path not in _RATE_LIMIT_EXEMPT_PATHS:
            limiter = getattr(app.state, "rate_limiter", None)
            if limiter is not None:
                decision: RateLimitDecision = await limiter.acquire(
                    _client_identity(request)
                )
                RATE_LIMIT_DECISIONS_TOTAL.labels(
                    decision=_decision_label(decision)
                ).inc()
                if not decision.allowed:
                    status_code = ErrorRegistry.RATE_LIMIT.http_status
                    envelope = DomainError(ErrorRegistry.RATE_LIMIT)
                    return JSONResponse(
                        status_code=status_code,
                        content=to_response_payload(envelope),
                        headers={"Retry-After": str(decision.retry_after_seconds)},
                    )

        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        HTTP_REQUESTS_TOTAL.labels(
            method=method, status_code=str(status_code)
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method).observe(
            time.perf_counter() - start
        )


def _decision_label(decision: RateLimitDecision) -> str:
    """把判定收敛成低基数标签（degraded 单独计，避免和正常放行混淆）。"""
    if decision.degraded:
        return "degraded"
    return "allowed" if decision.allowed else "denied"


def _client_identity(request: Request) -> str:
    """限流身份：带令牌按令牌指纹，匿名按 IP。

    指纹只做 SHA-256 截断，不可逆且不含原文，既能分桶又不会把凭证写进 Redis
    键名或日志。
    """
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        digest = hashlib.sha256(authorization[7:].encode("utf-8")).hexdigest()
        return f"token:{digest[:16]}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """统一请求日志 + trace_id 注入（可观测性 A3）。

    - trace_id 优先取上游传入的 x-trace-id，否则自动生成，并写入 contextvars，
      与 OpenTelemetry 的 span 上下文共同贯通同一次请求。
    - 记录 method/path/status/耗时，替代 uvicorn 默认 access log。
    """
    trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
    set_trace_id(trace_id)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # 未处理异常路径也必须留痕：否则错误请求没有访问日志、没有 trace 回写。
        logger.error(
            "http_request_error",
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            exc_info=True,
        )
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["x-trace-id"] = trace_id
    return response


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    """将领域异常转换为稳定的结构化错误码响应（D2 无副作用）。"""
    logger.warning(
        "domain_error",
        code=exc.error_code.code,
        category=exc.error_code.category.value,
        detail=exc.detail,
    )
    return JSONResponse(
        status_code=exc.error_code.http_status,
        content=to_response_payload(exc),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """把框架层参数校验失败统一到 C.3.2 错误信封（原先走 FastAPI 默认 detail 格式）。

    只透出 loc/msg/type，不含原始输入值（避免回显用户数据）。
    """
    errors = [
        {
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg", ""),
            "type": item.get("type", ""),
        }
        for item in exc.errors()
    ]
    logger.info("request_validation_error", errors=errors)
    envelope = DomainError(
        ErrorRegistry.VALIDATION_ERROR,
        detail="请求参数校验失败",
        context={"errors": errors},
    )
    return JSONResponse(
        status_code=ErrorRegistry.VALIDATION_ERROR.http_status,
        content=to_response_payload(envelope),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常统一为 INTERNAL_ERROR 信封（不向客户端泄漏堆栈与实现细节）。"""
    logger.error("unhandled_error", path=request.url.path, exc_info=exc)
    envelope = DomainError(ErrorRegistry.INTERNAL_ERROR)
    return JSONResponse(
        status_code=ErrorRegistry.INTERNAL_ERROR.http_status,
        content=to_response_payload(envelope),
    )


def _errors_catalog_allowed(token: str | None) -> bool:
    """错误码目录的访问判定（默认拒绝）。

    用 ``hmac.compare_digest`` 而不是 ``==``：常量时间比较，避免把「前缀匹配
    到第几位」变成可观测的时间差。令牌未配置时直接拒绝——空令牌绝不能等于
    「谁都能进」。
    """
    if settings.errors_catalog_public:
        return True
    expected = settings.errors_catalog_token.get_secret_value()
    if not expected or token is None:
        return False
    return hmac.compare_digest(token, expected)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """存活探针：只证明进程能应答，不触碰任何外部依赖。

    与 /readyz 严格分工：编排系统用 /health 判「要不要重启进程」，
    用 /readyz 判「能不能接流量」。
    """
    return {"status": "ok", "service": "api", "version": settings.app_version}


@app.get("/readyz", tags=["system"])
async def readyz() -> JSONResponse:
    """就绪探针：真实探测数据库可达性（SELECT 1），失败即 503。

    失败关闭（fail-closed）：数据库不可达时宁可把实例摘出负载均衡，
    也不让流量进来变成 500。响应体固定为最小状态，不泄漏连接串、
    角色名或底层错误——这是编排面协议，不进入业务错误信封。

    额外回报 ``schema_revision``（alembic 当前版本）：本机曾有两个 Postgres
    实例共用 15432 端口的不同地址族（127.0.0.1 → 旧集群、localhost → Docker），
    「连错库」表现为"迁移像是没跑"。把 revision 暴露在就绪面上，运维一眼可辨；
    读不到版本表同样按 unready 处理（fail-closed，不猜）。
    """
    try:
        # 就绪探针走身份域连接池：它只需要 SELECT 1 与 alembic_version，
        # 是权限最小的域。用它探活，等价于探"任意一个域能否连库"，
        # 又不会因为某个业务域的表权限配置错误而让探针说不清原因。
        pools = app.state.pools
        async with pools.sessionmaker(DatabaseDomain.IDENTITY)() as session:
            await session.execute(text("SELECT 1"))
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception:
        logger.error("readiness_check_failed", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"status": "unready", "checks": {"database": "down"}},
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "checks": {"database": "up"},
            "schema_revision": revision,
        },
    )


@app.get("/metrics", tags=["system"], include_in_schema=False)
async def metrics() -> Response:
    """Prometheus 抓取端点。

    不鉴权是刻意的：Prometheus 抓取通常不带业务凭证。它的暴露面由网络层
    控制（只允许监控网段访问），不放业务鉴权里做单点例外。
    """
    return metrics_response()


# JWKS 文档响应的缓存时长：外部验签方在此间隔内可放心使用本地副本，
# 轮换时最迟一个 max-age 拾取新公钥（与默认刷新间隔同一量级）。
_JWKS_CACHE_MAX_AGE_SECONDS = 300


@app.get("/.well-known/jwks.json", tags=["system"], include_in_schema=False)
async def jwks_document(
    if_none_match: str | None = Header(default=None),
) -> Response:
    """发布验签公钥集（RFC 7517 JWKS，ADR-R29）。

    只含公钥：文档来自 ``get_public_key_store()``——与验签路径同一个来源、
    同一份事实，不存在"发布的公钥"与"验签的公钥"两个版本。发布、验签
    同源意味着热轮换后本端点无需任何额外动作就自动反映新公钥集。

    不鉴权是标准做法（公钥本身可公开分发），配合 ETag/304 与
    ``Cache-Control`` 让消费方按标准 HTTP 缓存语义刷新。
    """
    document = get_public_key_store().jwks()
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    etag = f'"sha256:{hashlib.sha256(body).hexdigest()}"'
    headers = {
        "ETag": etag,
        "Cache-Control": f"public, max-age={_JWKS_CACHE_MAX_AGE_SECONDS}",
    }
    if if_none_match is not None and if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@app.get("/errors/{code}", tags=["system"], include_in_schema=False)
async def error_catalog(
    code: str,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    """错误码探测端点：返回指定错误码的定义（内部调试面，默认关闭）。

    访问控制（fail-closed）：
    - `errors_catalog_public=true` 才公开（本地/联调用，显式开关，不靠环境名推断）；
    - 否则必须配置 `ERRORS_CATALOG_TOKEN` 并在请求头带 `X-Internal-Token` 且完全一致；
    - 任何不满足的情况一律返回 NOT_FOUND —— 与「错误码不存在」不可区分，
      不给探测者「这里有个内部端点」的信号。
    """
    if not _errors_catalog_allowed(x_internal_token):
        raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"未知错误码: {code}")
    # 只允许访问 ErrorCode 注册项（按 code 精确匹配），拒绝 dunder 及类成员。
    target = next(
        (
            member
            for name, member in vars(ErrorRegistry).items()
            if not name.startswith("_") and isinstance(member, ErrorCode) and member.code == code
        ),
        None,
    )
    if target is None:
        raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"未知错误码: {code}")
    return {
        "code": target.code,
        "category": target.category.value,
        "http_status": target.http_status,
        "message": target.message,
        "retryable": target.retryable,
    }
