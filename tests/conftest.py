"""pytest 全局配置。

策略：
- 领域模型单元测试：纯内存，无外部依赖。
- API 集成测试：依赖真实 PostgreSQL（docker-compose 已起，符合 A1 可复现）。
- 事件循环：由 pytest-asyncio 的 session 级 loop 管理（见 pyproject.toml 的
  `asyncio_default_fixture_loop_scope` / `asyncio_default_test_loop_scope`），
  确保 asyncpg 连接池跨用例复用同一事件循环，避免跨循环失效。
  不再自定义旧版 `event_loop` fixture（pytest-asyncio>=0.23 已弃用该写法）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
