"""FastAPI 应用入口（apps/api）。

职责（薄层原则）：
- 装配 FastAPI 应用、注册异常处理器与中间件。
- 提供健康检查与错误码探测端点。
- 业务逻辑下沉到 services，本层只做协议适配。

契约来源：contracts/openapi/openapi.draft.yaml（后续逐步对齐路由）。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from packages.errors.error_codes import (
    DomainError,
    ErrorCode,
    ErrorRegistry,
    to_response_payload,
)
from packages.shared.config import get_settings
from packages.shared.database import lifespan
from packages.shared.logging import get_logger, set_trace_id, setup_logging
from packages.shared.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    RATE_LIMIT_DECISIONS_TOTAL,
    metrics_response,
)
from packages.shared.rate_limit import RateLimitDecision
from packages.telemetry.tracing import setup_telemetry

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("apps.api")

app = FastAPI(
    title="AI Creative Media Industrial OS",
    version=settings.app_version,
    description="AI 影视工业化生产平台 API（模块化单体 + 异步 Worker）",
    lifespan=lifespan,
)

# 接入 OpenTelemetry 分布式追踪（未配置 OTLP endpoint 时优雅降级）。
setup_telemetry(app)

# 挂载领域模块路由。
from services.project_planning.api.episode_routes import router as episode_router  # noqa: E402
from services.project_planning.api.project_routes import router as project_router  # noqa: E402
from services.project_planning.api.script_routes import router as script_router  # noqa: E402

app.include_router(project_router)
app.include_router(script_router)
app.include_router(episode_router)


# 探针与指标端点不参与限流：编排系统高频抓取，被限流会误判实例不可用。
_PROBE_PATHS = frozenset({"/health", "/readyz", "/metrics"})


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
        if request.url.path not in _PROBE_PATHS:
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
    """
    try:
        sessionmaker = app.state.sessionmaker
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.error("readiness_check_failed", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"status": "unready", "checks": {"database": "down"}},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "checks": {"database": "up"}},
    )


@app.get("/metrics", tags=["system"], include_in_schema=False)
async def metrics() -> Response:
    """Prometheus 抓取端点。

    不鉴权是刻意的：Prometheus 抓取通常不带业务凭证。它的暴露面由网络层
    控制（只允许监控网段访问），不放业务鉴权里做单点例外。
    """
    return metrics_response()


@app.get("/errors/{code}", tags=["system"])
async def error_catalog(code: str) -> dict[str, Any]:
    """错误码探测端点：返回指定错误码的定义（用于契约自检与联调）。

    生产环境关闭目录暴露：一律按 NOT_FOUND 处理，与未知错误码不可区分，
    避免把内部错误码清单当作调试面公开。
    """
    if settings.app_env == "production":
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
