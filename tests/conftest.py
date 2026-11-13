"""pytest 全局配置。

策略：
- 领域模型单元测试：纯内存，无外部依赖。
- API 集成测试：依赖真实 PostgreSQL（docker-compose 已起，符合 A1 可复现）。
- 事件循环：session 级复用，避免 asyncpg 连接池跨循环失效。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def event_loop():
    """session 级事件循环，供 asyncpg 连接池跨用例复用。"""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()
