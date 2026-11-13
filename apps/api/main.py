"""FastAPI 应用入口（apps/api）。

职责（薄层原则）：
- 装配 FastAPI 应用、注册异常处理器与中间件。
- 提供健康检查与错误码探测端点。
- 业务逻辑下沉到 services，本层只做协议适配。

契约来源：contracts/openapi/openapi.draft.yaml（后续逐步对齐路由）。
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from packages.errors.error_codes import DomainError, ErrorRegistry, to_response_payload
from packages.shared.config import get_settings
from packages.shared.logging import get_logger, set_trace_id, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("apps.api")

app = FastAPI(
    title="AI Creative Media Industrial OS",
    version=settings.app_version,
    description="AI 影视工业化生产平台 API（模块化单体 + 异步 Worker）",
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """统一请求日志 + trace_id 注入（可观测性 A3）。

    替代 uvicorn 默认 access log，统一记录 method/path/status/耗时，
    并保证同一次请求的应用日志与访问日志共用同一 trace_id。
    """
    trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
    set_trace_id(trace_id)

    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
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


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """健康检查：返回服务与版本信息。"""
    return {"status": "ok", "service": "api", "version": settings.app_version}


@app.get("/errors/{code}", tags=["system"])
async def error_catalog(code: str) -> dict:
    """错误码探测端点：返回指定错误码的定义（用于契约自检与联调）。"""
    registry = ErrorRegistry()
    target = getattr(registry, code, None)
    if target is None:
        raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"未知错误码: {code}")
    return {
        "code": target.code,
        "category": target.category.value,
        "http_status": target.http_status,
        "message": target.message,
        "retryable": target.retryable,
    }
