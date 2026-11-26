"""启动期身份断言的真库验收（WP-B）。

两条断言的价值不在「函数被调用过」，而在**它们真的会拦下错误的身份**：

* 发现身份：``qc_scheduler`` 通过；超级用户（能执行一切维护命令）必须失败。
* 维护身份：``qc_maintenance_login`` 通过；超级用户（能直读全部表）必须失败。

用真实凭据跑，而不是构造假引擎：身份断言的输入就是连接身份本身，
换成一个桩就等于测了另一个东西。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.database import (
    build_migration_url,
    create_qc_maintenance_engine,
    create_qc_scheduler_engine,
)
from workers.qc.scheduler import DiscoveryIdentityError, verify_discovery_identity
from workers.qc_maintenance.__main__ import verify_isolation


@pytest.fixture(scope="module")
async def admin_engine() -> Any:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_discovery_identity_passes_with_its_own_credentials() -> None:
    engine = create_qc_scheduler_engine()
    try:
        await verify_discovery_identity(engine)
    finally:
        await engine.dispose()


async def test_discovery_identity_refuses_superuser(admin_engine) -> None:
    with pytest.raises(DiscoveryIdentityError, match="超级用户"):
        await verify_discovery_identity(admin_engine)


async def test_maintenance_identity_passes_with_its_own_credentials() -> None:
    engine = create_qc_maintenance_engine()
    try:
        await verify_isolation(engine)
    finally:
        await engine.dispose()


async def test_maintenance_identity_refuses_superuser(admin_engine) -> None:
    with pytest.raises(RuntimeError) as caught:
        await verify_isolation(admin_engine)
    message = str(caught.value)
    assert "超级用户" in message
    assert "qc_tasks" in message, "高权身份同时必然能直读队列表，两条都要报出来"
