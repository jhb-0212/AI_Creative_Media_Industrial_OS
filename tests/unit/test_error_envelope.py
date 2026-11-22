"""统一错误信封测试（C.3.2）：框架层异常不得绕过 error_code 信封。

覆盖两条此前漏掉的通道：
- ``RequestValidationError``（422）：原先走 FastAPI 默认 ``{"detail": ...}``；
- 未预期异常（500）：原先直接冒泡，且无结构化字段。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from apps.api.main import unhandled_error_handler


@pytest.mark.anyio
async def test_validation_error_uses_envelope(auth_client: AsyncClient) -> None:
    """非法请求体返回 422，且使用统一信封（不是 FastAPI 默认 detail 格式）。"""
    response = await auth_client.post("/projects", json={"project_name": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert "detail" not in body
    assert isinstance(body["correlation_id"], str)


@pytest.mark.anyio
async def test_unhandled_error_uses_envelope() -> None:
    """未预期异常返回 500 INTERNAL_ERROR 信封，不泄漏堆栈。"""
    request = SimpleNamespace(url=SimpleNamespace(path="/boom"))
    response = await unhandled_error_handler(request, RuntimeError("boom"))

    assert response.status_code == 500
    body: dict[str, Any] = json.loads(response.body)
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "boom" not in json.dumps(body)
