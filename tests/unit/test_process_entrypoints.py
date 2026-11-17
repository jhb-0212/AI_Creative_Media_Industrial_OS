"""进程边界契约测试（审查 P1-04 / R7：配置读取与进程身份必须同源）。

这三条断言把「进程入口用哪份配置」变成 CI 可校验的事实，而不是靠人工检查：

1. 每个进程入口都必须经 ``get_process_settings(role)`` 声明身份——它同时
   执行该角色的生产密钥校验（fail-closed 的唯一落点）。入口绕过它，就等于
   「生产缺密钥仍能启动」，这是最容易被悄悄改坏的不变量。
2. 基础设施代码（DSN 构造）必须能**显式接收**进程配置，不得内部偷偷读全局
   配置——否则 relay 构造自己的 DSN 时会被 api 角色的密钥要求拖垮（P1-04）。
3. ``build_*_database_url(settings)`` 的角色名必须来自传入的配置，而不是
   固定字符串（R6：角色名单一事实源）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from packages.shared import database
from packages.shared.config import Settings

ROOT = Path(__file__).resolve().parents[2]

# 进程入口 → 它必须声明的角色。新增进程时这里必须加一行（否则等于新进程
# 偷偷绕过了密钥校验；R0 的同类防呆）。
ENTRYPOINTS: dict[str, str] = {
    "apps/api/main.py": "api",
    "workers/relay/__main__.py": "relay",
    "workers/outbox_maintenance/__main__.py": "outbox_maintenance",
    "workers/identity/app.py": "identity",
}


@pytest.mark.parametrize(("rel_path", "role"), sorted(ENTRYPOINTS.items()))
def test_entrypoint_declares_process_role(rel_path: str, role: str) -> None:
    """每个进程入口都显式声明自身角色（触发该角色的生产密钥校验）。"""
    source = (ROOT / rel_path).read_text(encoding="utf-8")
    assert "get_process_settings(" in source, (
        f"{rel_path} 未通过 get_process_settings 声明进程身份："
        "生产密钥校验就不会执行（缺密钥可启动）"
    )
    assert f'"{role}"' in source, f"{rel_path} 未声明预期角色 {role}"


@pytest.mark.parametrize(
    "builder_name",
    [
        "build_database_url",
        "build_migration_url",
        "build_relay_database_url",
        "build_project_planning_database_url",
        "build_asset_service_database_url",
    ],
)
def test_dsn_builder_accepts_process_settings(builder_name: str) -> None:
    """DSN 构造器必须能接收进程配置（连接角色与进程身份同源）。"""
    builder = getattr(database, builder_name)
    params = inspect.signature(builder).parameters
    assert "settings" in params, (
        f"{builder_name} 不能接收 settings：只能回落全局配置，"
        "跨进程调用时会读到别的进程的配置（P1-04）"
    )


def test_dsn_uses_settings_not_global_role_names() -> None:
    """角色名来自传入的 Settings，而非函数内硬编码（R6 单一事实源）。"""
    custom = Settings(
        _env_file=None,
        postgres_relay_user="custom_relay",
        postgres_relay_password="custom-pass",
        postgres_host="db.internal",
        postgres_port=6543,
        postgres_db="custom_db",
    )
    url = database.build_relay_database_url(custom)
    assert url.startswith("postgresql+asyncpg://custom_relay:custom-pass@")
    assert "db.internal:6543/custom_db" in url


async def test_relay_engine_shares_process_context() -> None:
    """create_relay_engine(settings) 用同一份上下文（不另建池参数/不换角色）。"""
    custom = Settings(
        _env_file=None,
        postgres_relay_user="relay_ctx",
        postgres_relay_password="p",
    )
    engine = database.create_relay_engine(custom)
    try:
        # 引擎的 URL 里必须是传入配置的角色，证明连接上下文未漂移。
        assert "relay_ctx" in str(engine.url)
        assert engine.pool.size() == 10  # 与 _make_engine 的连接池口径一致
    finally:
        await engine.dispose()
